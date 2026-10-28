#!/usr/bin/env bash
# wg-setup.sh — Set up this Debian/Ubuntu server as a WireGuard VPN server.
# Client traffic exits through Cloudflare WARP (--exit warp, default) or
# straight out this server's own IP (--exit direct).
# Clients are managed with wg-client.sh. See README.md.

set -Eeuo pipefail

PROG=$(basename "$0")
SELF=$(readlink -f "${BASH_SOURCE[0]}")
# shellcheck source=lib/common.sh
. "$(dirname "$SELF")/lib/common.sh"

# Print help.
usage() {
    cat <<EOF
Usage: $PROG [options] <command>

Sets up this server as a WireGuard VPN server. Order:
  --exit warp (default):  $PROG warp   (check its tests)   then  $PROG server
  --exit direct:          $PROG --exit direct server
Then add clients with wg-client.sh.

Commands:
  warp        Install deps + wgcf, register a free WARP account, write
              $WG_DIR/$WARP_IF.conf (Table = off), bring it up, run the WARP tests
  server      Create $WG_IF: routing/NAT out through WARP or this server's IP,
              firewall, IPv4 forwarding, systemd units, server keys
  test        All verification checks; exits non-zero on any FAIL
  status      Interfaces, handshakes, routes, rules
  backup [dir]  Archive keys, configs and clients (default dir /root/wg-backups)
  rollback    Stop/disable the interfaces, remove all rules, restore original
              files; keeps keys and clients unless --purge
  help        This text

Options:
  --exit warp|direct    Where client traffic exits (saved; switch via rollback)
  --dry-run             Show what would change; change nothing
  --yes, -y             Don't prompt before network changes
  --force               Regenerate existing keys, reinstall wgcf, regenerate the
                        WARP profile, change the subnet with clients present, or
                        take over configs not made by wireguard-tools
  --purge               With rollback: also delete keys, clients, WARP account

Config (flag / environment variable; saved by warp/server for later runs):
  --exit         WG_EXIT              $EXIT
  --subnet       WG_SUBNET            $WG_SUBNET
  --port         WG_PORT              $WG_PORT
  --mtu          WG_MTU               $WG_MTU  (default 1280 for warp, 1420 for direct)
  --warp-mtu     WARP_MTU             $WARP_MTU
  --dns          WG_DNS               $WG_DNS  (DNS server given to clients)
  --endpoint     ENDPOINT             ${ENDPOINT:-<auto-detected public IPv4>}
  --wg-if        WG_IF                $WG_IF
  --warp-if      WARP_IF              $WARP_IF
  --table        RT_TABLE             $RT_TABLE  (warp policy-routing table)
  --allowed-ips  CLIENT_ALLOWED_IPS   $CLIENT_ALLOWED_IPS  (default for new clients)
                 WGCF_VERSION         $WGCF_VERSION  (e.g. v2.2.22)
                 WGCF_SHA256          ${WGCF_SHA256:-<unset>}  (pin the wgcf binary)
EOF
}

# --- warp ---

# Install missing packages (qrencode and magic-wormhole are for wg-client.sh
# --qr and share).
install_packages() {
    local p missing=()
    for p in wireguard wireguard-tools iptables curl ca-certificates qrencode magic-wormhole; do
        [ "$(dpkg-query -W -f='${Status}' "$p" 2>/dev/null || true)" = "install ok installed" ] || missing+=("$p")
    done
    if [ ${#missing[@]} -eq 0 ]; then
        info "packages present"
        return 0
    fi
    step "Install packages: ${missing[*]}" "apt-get remove ${missing[*]}"
    run apt-get update
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
}

# Download wgcf for this architecture from GitHub, check it, install it.
install_wgcf() {
    if [ -x "$WGCF_BIN" ] && "$WGCF_BIN" --help >/dev/null 2>&1 && [ "$FORCE" -eq 0 ]; then
        info "wgcf present: $WGCF_BIN"
        return 0
    fi
    step "Install wgcf ($WGCF_VERSION, linux_$ARCH) to $WGCF_BIN" "rm $WGCF_BIN"
    if [ "$DRY_RUN" -eq 1 ]; then
        info "[dry-run] would download from github.com/ViRb3/wgcf releases"
        return 0
    fi
    local api json url sums_url want got
    if [ "$WGCF_VERSION" = latest ]; then
        api=https://api.github.com/repos/ViRb3/wgcf/releases/latest
    else
        api="https://api.github.com/repos/ViRb3/wgcf/releases/tags/$WGCF_VERSION"
    fi
    json=$(curl -fsSL --max-time 30 "$api") || die "cannot reach GitHub releases API ($api)"
    url=$(grep -oE '"browser_download_url": *"[^"]+"' <<<"$json" | grep -oE 'https://[^"]+' | grep -E "_linux_${ARCH}\$" | head -n 1 || true)
    [ -n "$url" ] || die "no linux_$ARCH asset in wgcf release $WGCF_VERSION"
    sums_url=$(grep -oE '"browser_download_url": *"[^"]+"' <<<"$json" | grep -oE 'https://[^"]+' | grep -iE 'checksums|sha256' | head -n 1 || true)
    curl -fsSL --max-time 120 -o "$TMPD/wgcf" "$url" || die "download failed: $url"

    got=$(sha256sum "$TMPD/wgcf" | awk '{ print $1 }')
    if [ -n "$WGCF_SHA256" ]; then
        want="$WGCF_SHA256"
    elif [ -n "$sums_url" ]; then
        want=$(curl -fsSL --max-time 30 "$sums_url" | awk -v f="$(basename "$url")" '$2 == f || $2 == "*" f { print $1 }' || true)
    else
        want=""
    fi
    if [ -n "$want" ]; then
        [ "$got" = "$want" ] || die "wgcf checksum mismatch (got $got, want $want)"
        info "sha256 verified: $got"
    else
        warn "release publishes no checksums; set WGCF_SHA256=$got to pin this binary"
    fi
    chmod 755 "$TMPD/wgcf"
    "$TMPD/wgcf" --help >/dev/null 2>&1 || die "downloaded wgcf does not run"
    install -m 755 "$TMPD/wgcf" "$WGCF_BIN"
    info "installed $(basename "$url")"
}

# Register a WARP account and generate the WireGuard profile (once).
register_warp() {
    local log="$WGCF_DIR/wgcf.log"
    if [ -f "$WGCF_DIR/wgcf-account.toml" ]; then
        info "WARP account exists ($WGCF_DIR/wgcf-account.toml); not re-registering"
    else
        step "Register a free WARP account (accepts Cloudflare's WARP terms)" "rm -rf $WGCF_DIR"
        if [ "$DRY_RUN" -eq 0 ]; then
            ( cd "$WGCF_DIR" && umask 077 && "$WGCF_BIN" register --accept-tos ) >"$log" 2>&1 \
                || die "wgcf register failed; see $log. WARP may refuse registrations from some server IPs:
       retry later, or register on another machine and copy wgcf-account.toml and
       wgcf-profile.conf into $WGCF_DIR/, then re-run '$PROG warp' (see README)"
        fi
    fi
    if [ -f "$WGCF_DIR/wgcf-profile.conf" ] && [ "$FORCE" -eq 0 ]; then
        info "WARP profile exists"
        return 0
    fi
    step "Generate WARP WireGuard profile" "rm $WGCF_DIR/wgcf-profile.conf"
    if [ "$DRY_RUN" -eq 0 ]; then
        ( cd "$WGCF_DIR" && umask 077 && "$WGCF_BIN" generate ) >>"$log" 2>&1 \
            || die "wgcf generate failed; see $log"
        chmod 600 "$WGCF_DIR"/*
    fi
}

# Render warp.conf from the wgcf profile: Table = off, our MTU, no DNS, no
# IPv6 entries without IPv6, IPv4 endpoint on IPv4-only hosts, keepalive, and
# loose rp_filter (strict would drop WARP's replies; not routing/NAT).
render_warp_conf() {
    local profile="$1" v6=0 ep host port ip4
    if has_ipv6; then v6=1; fi
    ep=$(awk '/^[[:space:]]*Endpoint[[:space:]]*=/ { sub(/^[^=]*=[[:space:]]*/, ""); print; exit }' "$profile")
    if [ "$v6" -eq 0 ] && [ -n "$ep" ]; then
        host="${ep%:*}"
        port="${ep##*:}"
        if ! [[ $host =~ ^[0-9.]+$ ]]; then
            ip4=$(getent ahostsv4 "$host" | awk 'NR == 1 { print $1 }' || true)
            if [ -n "$ip4" ]; then ep="$ip4:$port"; else warn "could not resolve $host to IPv4; keeping it"; fi
        fi
    fi
    awk -v v6="$v6" -v mtu="$WARP_MTU" -v ep="$ep" -v marker="$MARKER" '
        function addrs(key, val,   n, i, a, out) {
            n = split(val, a, /[[:space:]]*,[[:space:]]*/)
            out = ""
            for (i = 1; i <= n; i++) {
                if (a[i] == "" || (!v6 && a[i] ~ /:/)) continue
                out = out (out == "" ? "" : ", ") a[i]
            }
            if (out != "") print key " = " out
        }
        BEGIN { print marker " from wgcf-profile.conf; no routing/NAT here (see wg0.conf)" }
        /^[[:space:]]*\[Interface\]/ {
            print "[Interface]"; print "MTU = " mtu; print "Table = off"
            print "PostUp = sysctl -q -w net.ipv4.conf.%i.rp_filter=2"
            sec = "i"; next
        }
        /^[[:space:]]*\[Peer\]/ { print ""; print "[Peer]"; sec = "p"; next }
        /^[[:space:]]*(#|$)/ { next }
        {
            k = $0; sub(/[[:space:]]*=.*/, "", k); gsub(/^[[:space:]]+/, "", k)
            v = $0; sub(/^[^=]*=[[:space:]]*/, "", v); sub(/[[:space:]]+$/, "", v)
        }
        sec == "i" && (k == "DNS" || k == "MTU" || k == "Table" || k ~ /^(Pre|Post)(Up|Down)$/) { next }
        k == "Address" || k == "AllowedIPs" { addrs(k, v); next }
        k == "Endpoint" { print "Endpoint = " ep; next }
        k == "PersistentKeepalive" { next }
        { print k " = " v }
        END { print "PersistentKeepalive = 25" }
    ' "$profile"
}

# Write warp.conf; sets WARP_CHANGED.
write_warp_conf() {
    local profile="$WGCF_DIR/wgcf-profile.conf"
    WARP_CHANGED=0
    if [ ! -f "$profile" ]; then
        [ "$DRY_RUN" -eq 1 ] || die "missing $profile"
        info "[dry-run] would write $WARP_CONF from $profile (Table = off, MTU $WARP_MTU, no DNS)"
        return 0
    fi
    ensure_managed "$WARP_CONF"
    write_file "$WARP_CONF" 600 < <(render_warp_conf "$profile")
    WARP_CHANGED=$WROTE
}

cmd_warp() {
    [ "$EXIT" = warp ] || die "--exit direct doesn't use WARP; run '$PROG --exit direct server'"
    take_lock
    # Create the WARP account folder first, so account files registered on
    # another machine can be copied in even if this run fails later.
    if [ "$DRY_RUN" -eq 0 ]; then install -d -m 700 "$WGCF_DIR"; fi
    save_config
    record_baseline
    install_packages
    install_wgcf
    register_warp
    write_warp_conf
    start_unit "$WARP_IF" "$WARP_CHANGED"
    guard_default_route
    if [ "$DRY_RUN" -eq 1 ]; then
        log "Dry run: skipping WARP tests"
        return 0
    fi
    log "WARP tests"
    run_tests warp
    report_tests
    info "Next: $PROG server"
}

# --- server ---

# Create the server keypair unless it exists (--force regenerates).
ensure_server_keys() {
    KEYS_CHANGED=0
    if [ -s "$SERVER_KEY" ] && [ -s "$SERVER_PUB" ] && [ "$FORCE" -eq 0 ]; then
        info "server keys present"
        return 0
    fi
    if [ -s "$SERVER_KEY" ]; then
        step "Regenerate server keys (--force): every client must be re-issued (wg-client.sh rotate)" \
            "restore $SERVER_KEY from its .bak"
        if [ "$DRY_RUN" -eq 0 ]; then backup_file "$SERVER_KEY"; backup_file "$SERVER_PUB"; fi
    else
        step "Generate server keypair ($SERVER_KEY)" "rm $SERVER_KEY $SERVER_PUB"
    fi
    KEYS_CHANGED=1
    [ "$DRY_RUN" -eq 0 ] || return 0
    ( umask 077 && wg genkey >"$SERVER_KEY.new" && mv -f "$SERVER_KEY.new" "$SERVER_KEY" )
    wg pubkey <"$SERVER_KEY" >"$SERVER_PUB"
    chmod 600 "$SERVER_KEY" "$SERVER_PUB"
}

# ip_forward before install (current value if not recorded yet).
orig_forward() {
    local v
    v=$(state_get ORIG_IP_FORWARD)
    echo "${v:-$(sysctl -n net.ipv4.ip_forward)}"
}

# Persist and apply net.ipv4.ip_forward=1.
write_sysctl() {
    if [ ! -f "$SYSCTL_FILE" ] || [ "$(sysctl -n net.ipv4.ip_forward)" != 1 ]; then
        step "Enable IPv4 forwarding ($SYSCTL_FILE)" "$PROG rollback (restores ip_forward=$(orig_forward))"
        confirm "Enable IPv4 forwarding?"
    fi
    write_file "$SYSCTL_FILE" 644 <<EOF
$MARKER
net.ipv4.ip_forward = 1
EOF
    if [ "$(sysctl -n net.ipv4.ip_forward)" != 1 ]; then run sysctl -q -w net.ipv4.ip_forward=1; fi
}

# warp mode: order wg0 after warp at boot and stop it with warp.
write_dropin() {
    write_file "$DROPIN" 644 <<EOF
$MARKER
[Unit]
After=wg-quick@$WARP_IF.service
Requires=wg-quick@$WARP_IF.service
EOF
    if [ "$WROTE" -eq 1 ]; then run systemctl daemon-reload; fi
}

# ufw (if active): allow the WireGuard port and wg0 -> exit forwarding.
open_firewall() {
    local st out
    out=$(out_if)
    st=$(ufw status 2>/dev/null || true)
    if [[ $st == *"Status: active"* ]]; then
        step "ufw: allow $WG_PORT/udp and routing $WG_IF -> $out" \
            "ufw delete allow $WG_PORT/udp; ufw route delete allow in on $WG_IF out on $out"
        run ufw allow "$WG_PORT/udp" comment wireguard-tools
        run ufw route allow in on "$WG_IF" out on "$out" comment wireguard-tools
        if [ "$(state_get UFW)" != 1 ]; then state_set UFW 1; fi
    else
        info "ufw not active; $WG_IF's PostUp opens udp/$WG_PORT in iptables INPUT"
    fi
}

cmd_server() {
    take_lock
    if [ "$EXIT" = warp ]; then
        [ -f "$WARP_CONF" ] || [ "$DRY_RUN" -eq 1 ] || die "no $WARP_CONF; run '$PROG warp' first (or use --exit direct)"
        if_exists "$WARP_IF" || [ "$DRY_RUN" -eq 1 ] || die "$WARP_IF is down; run '$PROG warp' first"
    fi
    if [ -n "$SAVED_SUBNET" ] && [ "$SAVED_SUBNET" != "$WG_SUBNET" ] && [ -n "$(db_entries)" ] && [ "$FORCE" -eq 0 ]; then
        die "clients exist in $SAVED_SUBNET; changing to $WG_SUBNET breaks them (--force, then re-add every client)"
    fi
    ensure_managed "$WG_CONF"
    save_config
    record_baseline
    info "exit mode: $EXIT (client traffic leaves via $(out_if))"
    install_packages
    ensure_server_keys
    resolve_endpoint 1
    write_sysctl
    if [ "$EXIT" = warp ]; then write_dropin; fi
    write_wg_conf
    local changed="$WROTE"
    if [ "$KEYS_CHANGED" -eq 1 ]; then changed=1; fi
    open_firewall
    CLEANUP_ON_ERR=1
    start_unit "$WG_IF" "$changed"
    CLEANUP_ON_ERR=0
    guard_default_route
    echo ""
    info "REMINDER: open UDP $WG_PORT in your VPS provider's firewall / security group."
    info "Next: wg-client.sh add <name>"
}

# --- status ---

cmd_status() {
    local ifc unit active enabled t c r
    log "Mode: exit $EXIT (out via $(out_if))"
    log "Interfaces"
    for ifc in "$WARP_IF" "$WG_IF"; do
        if [ "$ifc" = "$WARP_IF" ] && [ "$EXIT" != warp ]; then continue; fi
        unit="wg-quick@$ifc"
        active=$(systemctl is-active "$unit" 2>/dev/null || true)
        enabled=$(systemctl is-enabled "$unit" 2>/dev/null || true)
        if if_exists "$ifc"; then
            info "$(ip -br addr show dev "$ifc")  [$active, ${enabled:-disabled}]"
        else
            info "$ifc: absent  [${enabled:-disabled}]"
        fi
    done
    log "Handshakes"
    if [ "$EXIT" = warp ] && if_exists "$WARP_IF"; then
        info "$WARP_IF: $(ago "$(wg show "$WARP_IF" latest-handshakes | awk '$2 > m { m = $2 } END { print m + 0 }')")"
    fi
    if if_exists "$WG_IF"; then
        local -A hs=()
        local k ts name ip pub _
        while read -r k ts; do hs[$k]="$ts"; done < <(wg show "$WG_IF" latest-handshakes)
        while read -r name ip pub _; do info "$name ($ip): $(ago "${hs[$pub]:-0}")"; done < <(db_entries)
    fi
    log "Routes"
    info "main default: $(default_route)  (recorded: $(state_get DEFAULT_ROUTE))"
    if [ "$EXIT" = warp ]; then
        ip rule show | grep -F "lookup $RT_TABLE" | sed 's/^/    rule: /' || true
        ip route show table "$RT_TABLE" 2>/dev/null | sed "s/^/    table $RT_TABLE: /" || true
    fi
    log "Rules"
    while IFS='|' read -r t c r; do
        # shellcheck disable=SC2086  # $r is a rule: split into iptables args
        if iptables -t "$t" -C "$c" $r 2>/dev/null; then info "present  -t $t $c $r"; else info "missing  -t $t $c $r"; fi
    done < <(ipt_rules)
    info "ip_forward = $(sysctl -n net.ipv4.ip_forward)"
}

# --- test ---

T_PASS=0 T_FAIL=0 T_WARN=0

t_result() {
    printf '  %-4s %s\n' "$1" "$2"
    case "$1" in
        PASS) T_PASS=$(( T_PASS + 1 )) ;;
        FAIL) T_FAIL=$(( T_FAIL + 1 )) ;;
        WARN) T_WARN=$(( T_WARN + 1 )) ;;
    esac
}
check() {
    local desc="$1"
    shift
    if "$@"; then t_result PASS "$desc"; else t_result FAIL "$desc"; fi
}

t_warp_on() {
    local w
    w=$(trace_field warp --interface "$WARP_IF")
    [ "$w" = on ] || [ "$w" = plus ]
}
t_warp_handshake() {
    local newest
    newest=$(wg show "$WARP_IF" latest-handshakes 2>/dev/null | awk '$2 > m { m = $2 } END { print m + 0 }')
    [ "$newest" -gt 0 ] && [ $(( $(date +%s) - newest )) -le 180 ]
}
t_untunneled() { [ "$(trace_field warp)" = off ]; }
t_default_route() {
    local rec
    rec=$(state_get DEFAULT_ROUTE)
    [ -n "$rec" ] && [ "$(route_key "$(default_route)")" = "$(route_key "$rec")" ]
}
t_listen() { [ "$(wg show "$WG_IF" listen-port 2>/dev/null)" = "$WG_PORT" ]; }
t_forward() { [ "$(sysctl -n net.ipv4.ip_forward)" = 1 ]; }
t_ip_rule() { [ "$(ip rule show | grep -cF "from $WG_SUBNET lookup $RT_TABLE")" -eq 1 ]; }
t_table() {
    local r
    r=$(ip route show table "$RT_TABLE" 2>/dev/null || true)
    grep -qE "^default dev $WARP_IF( |$)" <<<"$r" \
        && grep -qE "^blackhole default( |$)" <<<"$r" \
        && grep -qE "^${WG_SUBNET//./\\.} dev $WG_IF( |$)" <<<"$r"
}
# Exactly one copy of an iptables rule ("table|chain|rule"), via iptables -S.
t_ipt_once() {
    local t c r re
    IFS='|' read -r t c r <<<"$1"
    re=$(sed -E 's/\./\\./g; s/-p (tcp|udp) /-p \1 (-m \1 )?/' <<<"-A $c $r")
    [ "$(iptables -t "$t" -S "$c" 2>/dev/null | grep -cE "^${re}\$")" -eq 1 ]
}
t_enabled() {
    systemctl is-enabled -q "wg-quick@$WG_IF" || return 1
    [ "$EXIT" != warp ] || systemctl is-enabled -q "wg-quick@$WARP_IF"
}
t_ordering() {
    [[ " $(systemctl show -p After --value "wg-quick@$WG_IF") " == *" wg-quick@$WARP_IF.service "* ]] \
        && [[ " $(systemctl show -p Requires --value "wg-quick@$WG_IF") " == *" wg-quick@$WARP_IF.service "* ]]
}

# Run the checks; "warp" stops after the WARP checks.
run_tests() {
    local rule
    if [ "$EXIT" = warp ]; then
        check "$WARP_IF: curl --interface $WARP_IF shows warp=on" t_warp_on
        check "$WARP_IF: handshake within the last 3 minutes" t_warp_handshake
        check "server's own traffic is not tunneled (plain curl shows warp=off)" t_untunneled
    fi
    check "default route unchanged since install ($(state_get DEFAULT_ROUTE))" t_default_route
    [ "$1" != warp ] || return 0
    if [ ! -f "$WG_CONF" ]; then
        t_result SKIP "$WG_IF checks (run '$PROG server' first)"
        return 0
    fi
    check "$WG_IF listening on udp/$WG_PORT" t_listen
    check "net.ipv4.ip_forward = 1" t_forward
    if [ "$EXIT" = warp ]; then
        check "ip rule: from $WG_SUBNET lookup $RT_TABLE (exactly once)" t_ip_rule
        check "table $RT_TABLE: default via $WARP_IF, blackhole fallback, $WG_SUBNET via $WG_IF" t_table
    fi
    while IFS= read -r rule; do
        check "iptables exactly once: ${rule//|/ }" t_ipt_once "$rule"
    done < <(ipt_rules)
    check "wg-quick units enabled at boot" t_enabled
    if [ "$EXIT" = warp ]; then
        check "wg-quick@$WG_IF ordered after and requires wg-quick@$WARP_IF" t_ordering
    fi
    if wg show "$WG_IF" latest-handshakes 2>/dev/null | awk '$2 > 0 { f = 1 } END { exit !f }'; then
        t_result PASS "at least one client handshake"
    else
        t_result WARN "no client has connected yet"
    fi
}

report_tests() {
    echo ""
    info "$T_PASS passed, $T_FAIL failed, $T_WARN warnings"
    [ "$T_FAIL" -eq 0 ] || exit 1
}

cmd_test() {
    log "Tests (exit $EXIT)"
    run_tests all
    report_tests
}

# --- backup ---

# Archive keys, configs, clients and state (mode 600; it holds private keys).
cmd_backup() {
    local dir="${1:-/root/wg-backups}" out paths=() p
    for p in "$WG_DIR" "$CLIENT_DIR" "$SYSCTL_FILE" "$DROPIN_DIR"; do
        if [ -e "$p" ]; then paths+=("$p"); fi
    done
    [ ${#paths[@]} -gt 0 ] || die "nothing to back up"
    out="$dir/wireguard-tools.$(hostname -s).$(date +%Y%m%d-%H%M%S).tar.gz"
    step "Back up ${paths[*]} to $out" "rm $out"
    run install -d -m 700 "$dir"
    run tar -czf "$out" -P "${paths[@]}"
    run chmod 600 "$out"
    warn "the archive contains private keys; keep it somewhere safe"
    info "restore on a fresh server: tar -xzpf <archive> -P, then '$PROG server'"
    info "fetch it with: scp root@$(state_get ENDPOINT | tr -d '[]'):$out ."
}

# --- rollback ---

# Restore a file's original backup; files we created are removed if <rm_new> is 1.
restore_original() {
    local f="$1" rm_new="$2" orig
    orig=$(awk -F'\t' -v f="$f" '$1 == f { print $2; exit }' "$BACKUP_MAP" 2>/dev/null || true)
    case "$orig" in
        "") ;;
        NEW)
            if [ "$rm_new" -eq 1 ]; then
                run rm -f "$f"
            elif [ -e "$f" ]; then
                info "kept $f (created by wireguard-tools; --purge deletes it)"
            fi
            ;;
        *)
            if [ -e "$orig" ]; then run cp -a "$orig" "$f"; info "restored $f from $orig"
            else warn "original backup $orig is missing; left $f as is"; fi
            ;;
    esac
}

cmd_rollback() {
    take_lock
    local orig_fwd f out
    orig_fwd=$(state_get ORIG_IP_FORWARD)
    out=$(out_if)
    local ifs="$WG_IF"
    if [ "$EXIT" = warp ]; then ifs="$WG_IF and $WARP_IF"; fi
    step "Roll back (exit $EXIT): stop $ifs, remove routing/NAT/firewall rules, restore original files" \
        "$PROG warp && $PROG server (or --exit direct server)"
    if [ "$PURGE" -eq 1 ]; then
        warn "--purge also deletes the WARP account, server and client keys, and client configs (irreversible)"
    fi
    confirm "Proceed with rollback?"

    run systemctl disable --now "wg-quick@$WG_IF" 2>/dev/null || true
    run systemctl disable --now "wg-quick@$WARP_IF" 2>/dev/null || true
    if if_exists "$WG_IF"; then run ip link del dev "$WG_IF"; fi
    if if_exists "$WARP_IF"; then run ip link del dev "$WARP_IF"; fi
    run_hooks down

    if [ "$(state_get UFW)" = 1 ] && command -v ufw >/dev/null; then
        run ufw delete allow "$WG_PORT/udp" || true
        run ufw route delete allow in on "$WG_IF" out on "$out" || true
        state_unset UFW
    fi

    restore_original "$DROPIN" 1
    if [ -d "$DROPIN_DIR" ]; then run rmdir --ignore-fail-on-non-empty "$DROPIN_DIR"; fi
    run systemctl daemon-reload
    restore_original "$SYSCTL_FILE" 1
    if [ -n "$orig_fwd" ]; then run sysctl -q -w "net.ipv4.ip_forward=$orig_fwd"; fi
    restore_original "$WG_CONF" "$PURGE"
    restore_original "$WARP_CONF" "$PURGE"

    if [ "$PURGE" -eq 1 ]; then
        for f in "$WG_CONF" "$WARP_CONF" "$SERVER_KEY" "$SERVER_PUB" "$CLIENT_DB" "$SYSCTL_FILE"; do
            # shellcheck disable=SC2086  # glob on purpose: this file's .bak copies
            run rm -f $f.*.bak
        done
        run rm -f "$SERVER_KEY" "$SERVER_PUB" "$CLIENT_DB" "$STATE" "$BACKUP_MAP"
        run rm -rf "$WGCF_DIR" "$CLIENT_DIR" "$PSK_DIR"
        info "left installed: packages and $WGCF_BIN"
    else
        # Free the mode (and its MTU default) so the next setup can pick either.
        state_unset EXIT
        state_unset WG_MTU
        info "kept: keys, clients ($CLIENT_DB, $CLIENT_DIR), $WGCF_DIR (rollback --purge removes them)"
    fi
    log "Rollback done; the main routing table was never changed"
}

# --- main ---

main() {
    init "$@"
    set -- "${ARGS[@]}"
    local cmd="$1"
    shift
    case "$cmd" in
        warp)     cmd_warp ;;
        server)   cmd_server ;;
        test)     cmd_test ;;
        status)   cmd_status ;;
        backup)   cmd_backup "$@" ;;
        rollback) cmd_rollback ;;
        *) die "unknown command: $cmd (see: $PROG help)" ;;
    esac
}

main "$@"
