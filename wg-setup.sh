#!/usr/bin/env bash
# wg-setup.sh — Set up this Debian/Ubuntu server as a WireGuard VPN server.
# Two modes, each its own command: `direct` (client traffic exits via this
# server's IP) or `warp` (client traffic exits through Cloudflare WARP).
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

Sets up this server as a WireGuard VPN server. Pick one mode:

  $PROG direct   Clients' traffic exits via this server's own IP
  $PROG warp     Clients' traffic exits through Cloudflare WARP

Then add clients with wg-client.sh. Re-run the same command to apply changes
(e.g. --port); to switch modes, rollback first.

Commands:
  direct      Create $WG_IF: server keys, NAT out this server's public
              interface, firewall, IPv4 forwarding, systemd unit
  warp        First the WARP tunnel: install wgcf, register a free WARP account,
              bring up $WARP_IF (Table = off), run the WARP tests. If they pass,
              and you confirm, create $WG_IF routed out through $WARP_IF
  test        All verification checks; exits non-zero on any FAIL
  status      Interfaces, handshakes, routes, rules
  backup [dir]  Archive keys, configs and clients (default dir $BACKUP_DIR)
  restore <archive>
              Set this server up from a backup: same keys, clients, WARP
              account and settings, then runs direct or warp as before
  rollback    Stop/disable the interfaces, remove all rules, restore original
              files; keeps keys and clients unless --purge
  help        This text

Options:
  --dry-run             Show what would change; change nothing
  --yes, -y             Don't prompt before network changes
  --force               Regenerate existing keys, reinstall wgcf, regenerate the
                        WARP profile, change the subnet with clients present, or
                        take over configs not made by wireguard-tools
  --purge               With rollback: also delete keys, clients, WARP account

Config (flag / environment variable; saved by direct/warp for later runs):
  --name         SERVER_NAME          $SERVER_NAME  (client configs: <name>.<client>.conf)
  --subnet       WG_SUBNET            $WG_SUBNET
  --subnet6      WG_SUBNET6           ${WG_SUBNET6:-<random fd::/64 at setup>}  (client IPv6; "off" disables)
  --port         WG_PORT              $WG_PORT
  --mtu          WG_MTU               (default 1420 for direct, 1280 for warp)
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

# Render warp.conf from the wgcf profile: Table = off, our MTU, no DNS,
# WARP's IPv6 address kept only when client IPv6 goes through it, IPv4
# endpoint on IPv4-only hosts, keepalive, loose rp_filter (strict would drop
# WARP's replies), and RT_TABLE's default route(s) (they go when warp goes
# down; see lib/common.sh).
render_warp_conf() {
    local profile="$1" v6="$IPV6" ep host port ip4 routes
    ep=$(awk '/^[[:space:]]*Endpoint[[:space:]]*=/ { sub(/^[^=]*=[[:space:]]*/, ""); print; exit }' "$profile")
    if ! has_ipv6 && [ -n "$ep" ]; then
        host="${ep%:*}"
        port="${ep##*:}"
        if ! [[ $host =~ ^[0-9.]+$ ]]; then
            ip4=$(getent ahostsv4 "$host" | awk 'NR == 1 { print $1 }' || true)
            if [ -n "$ip4" ]; then ep="$ip4:$port"; else warn "could not resolve $host to IPv4; keeping it"; fi
        fi
    fi
    routes=$(warp_route | paste -sd'|' -)
    awk -v v6="$v6" -v mtu="$WARP_MTU" -v ep="$ep" -v marker="$MARKER" -v routes="$routes" '
        function addrs(key, val,   n, i, a, out) {
            n = split(val, a, /[[:space:]]*,[[:space:]]*/)
            out = ""
            for (i = 1; i <= n; i++) {
                if (a[i] == "" || (!v6 && a[i] ~ /:/)) continue
                out = out (out == "" ? "" : ", ") a[i]
            }
            if (out != "") print key " = " out
        }
        BEGIN { print marker " from wgcf-profile.conf; other routing/NAT is in wg0.conf" }
        /^[[:space:]]*\[Interface\]/ {
            print "[Interface]"; print "MTU = " mtu; print "Table = off"
            print "PostUp = sysctl -q -w net.ipv4.conf.%i.rp_filter=2"
            n = split(routes, r, "|"); for (i = 1; i <= n; i++) print "PostUp = " r[i]
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
    take_lock
    # Refuse a breaking change before saving config or touching WARP.
    check_server_change
    ensure_subnet6
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
        setup_server
        return 0
    fi
    log "WARP tests"
    run_tests warp
    if [ "$T_FAIL" -gt 0 ]; then
        info "fix WARP before going on (see '$PROG status'), then re-run '$PROG warp'"
        report_tests
    fi
    info "WARP is working ($T_PASS checks passed)"
    if ! continue_to_server; then
        info "stopped after WARP; re-run '$PROG warp' to set up the WireGuard server"
        return 0
    fi
    setup_server
}

# Ask whether to go on from the WARP tunnel to the WireGuard server.
continue_to_server() {
    if [ "$DRY_RUN" -eq 1 ] || [ "$YES" -eq 1 ]; then return 0; fi
    have_tty || return 1
    local reply=""
    echo ""
    read -r -p "    Set up the WireGuard server ($WG_IF) through $WARP_IF now? [Y/n] " reply </dev/tty || true
    [[ ! $reply =~ ^[Nn] ]]
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

# The forwarding sysctls to persist, as "key = value" lines. With IPv6
# forwarding on, a host that gets its own IPv6 from router advertisements
# would stop accepting them (and lose its IPv6 route) unless accept_ra is 2.
sysctl_lines() {
    local pub
    echo "net.ipv4.ip_forward = 1"
    [ "$IPV6" -eq 1 ] || return 0
    echo "net.ipv6.conf.all.forwarding = 1"
    pub=$(pub_if)
    if has_ipv6 && [ "$(sysctl -n "net.ipv6.conf.$pub.accept_ra" 2>/dev/null)" != 0 ]; then
        echo "net.ipv6.conf.$pub.accept_ra = 2"
    fi
}

# Persist and apply the forwarding sysctls.
write_sysctl() {
    local lines k v pub
    lines=$(sysctl_lines)
    pub=$(pub_if)
    # Remember accept_ra before we change it, so rollback can restore it.
    if [[ $lines == *accept_ra* ]] && [ -z "$(state_get ORIG_ACCEPT_RA)" ]; then
        state_set ORIG_ACCEPT_RA "$(sysctl -n "net.ipv6.conf.$pub.accept_ra")"
    fi
    if ! { [ -f "$SYSCTL_FILE" ] && [ "$(grep -v '^#' "$SYSCTL_FILE")" = "$lines" ]; }; then
        step "Enable forwarding ($SYSCTL_FILE: ${lines//$'\n'/; })" "$PROG rollback (restores ip_forward=$(orig_forward))"
        confirm "Enable forwarding?"
    fi
    write_file "$SYSCTL_FILE" 644 < <(echo "$MARKER"; echo "$lines")
    while IFS='=' read -r k v; do
        k="${k// /}"; v="${v// /}"
        if [ "$(sysctl -n "$k")" != "$v" ]; then run sysctl -q -w "$k=$v"; fi
    done <<<"$lines"
}

# Pick the client IPv6 /64 once (saved by save_config).
ensure_subnet6() {
    if [ -z "$WG_SUBNET6" ]; then
        WG_SUBNET6=$(random_subnet6)
        finalize_config
    fi
}

# warp mode: order wg0 after warp at boot and stop it with warp.
write_dropin() {
    local content
    content=$(render_template wg-quick-after-warp.conf.tmpl WG_IF="$WG_IF" WARP_IF="$WARP_IF")
    write_file "$DROPIN" 644 <<<"$content"
    if [ "$WROTE" -eq 1 ]; then run systemctl daemon-reload; fi
}

# Keep every client config named <server>.<client>.conf (renaming ones from an
# older server name, or from before server names), pointed at the current
# endpoint and port, and with its current addresses (IPv6 on or off). Keys are
# unchanged, so only the configs need re-sending.
sync_client_configs() {
    local want="$ENDPOINT:$WG_PORT" conf src old name ip _ addr stale=()
    # Configs are found via clients.db, so editing the template can't hide one.
    while read -r name ip _; do
        conf=$(client_conf "$name")
        src="$conf"
        addr=$(client_addrs "$ip" | sed 's/,/, /')
        if [ ! -f "$conf" ]; then
            for old in ${SAVED_SERVER_NAME:+"$CLIENT_DIR/$SAVED_SERVER_NAME.$name.conf"} "$CLIENT_DIR/$name.conf"; do
                if [ -f "$old" ]; then src="$old"; break; fi
            done
            [ -f "$src" ] || continue
            step "Rename $src to $(basename "$conf")" "mv $conf $src"
            run mv "$src" "$conf"
            if [ "$DRY_RUN" -eq 0 ]; then src="$conf"; fi
            stale+=("$name")
        fi
        grep -qxF "Endpoint = $want" "$src" && grep -qxF "Address = $addr" "$src" && continue
        write_file "$conf" 600 < <(sed -e "s|^Endpoint = .*|Endpoint = $want|" -e "s|^Address = .*|Address = $addr|" "$src")
        [[ " ${stale[*]} " == *" $name "* ]] || stale+=("$name")
    done < <(db_entries)
    if [ ${#stale[@]} -gt 0 ]; then
        warn "client configs changed (now $SERVER_NAME.<client>.conf at $want); re-send them (wg-client.sh share/show --qr): ${stale[*]}"
    fi
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

cmd_direct() {
    take_lock
    setup_server
}

# Refuse to change the subnet under existing clients, or to take over a
# wg0.conf we didn't create. Must run before save_config records the new values.
check_server_change() {
    if [ -n "$SAVED_SUBNET" ] && [ "$SAVED_SUBNET" != "$WG_SUBNET" ] && [ -n "$(db_entries)" ] && [ "$FORCE" -eq 0 ]; then
        die "clients exist in $SAVED_SUBNET; changing to $WG_SUBNET breaks them (--force, then re-add every client)"
    fi
    ensure_managed "$WG_CONF"
}

# The server must be set up (direct or warp); after rollback no mode is saved.
require_mode() {
    [ -n "$SAVED_EXIT" ] || die "not set up (no saved mode); run '$PROG direct' or '$PROG warp'"
}

# Create or update wg0 for the current mode (called by direct and warp).
setup_server() {
    if [ "$EXIT" = warp ]; then
        [ -f "$WARP_CONF" ] || [ "$DRY_RUN" -eq 1 ] || die "no $WARP_CONF; the WARP step didn't complete"
        if_exists "$WARP_IF" || [ "$DRY_RUN" -eq 1 ] || die "$WARP_IF is down; check '$PROG status', then re-run '$PROG warp'"
    fi
    check_server_change
    ensure_subnet6
    save_config
    record_baseline
    log "WireGuard server ($EXIT mode: client traffic leaves via $(out_if))"
    install_packages
    ensure_server_keys
    resolve_endpoint 1
    sync_client_configs
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
    local ifc unit active enabled x t c r f
    require_mode
    log "Mode: $EXIT (client traffic leaves via $(out_if))"
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
        while IFS= read -r f; do
            $f rule show | grep -F "lookup $RT_TABLE" | sed "s/^/    $f rule: /" || true
            $f route show table "$RT_TABLE" 2>/dev/null | sed "s/^/    $f table $RT_TABLE: /" || true
        done < <(ip_families)
    fi
    log "Rules"
    while IFS='|' read -r x t c r; do
        # shellcheck disable=SC2086  # $r is a rule: split into iptables args
        if $x -t "$t" -C "$c" $r 2>/dev/null; then info "present  $x -t $t $c $r"; else info "missing  $x -t $t $c $r"; fi
    done < <(ipt_rules)
    info "ip_forward = $(sysctl -n net.ipv4.ip_forward)"
    if [ "$IPV6" -eq 1 ]; then
        info "IPv6: clients in $WG_SUBNET6, ipv6 forwarding = $(sysctl -n net.ipv6.conf.all.forwarding)"
    else
        info "IPv6: off${WG_SUBNET6:+ ($( [ "$WG_SUBNET6" = off ] && echo disabled || echo "no IPv6 way out in $EXIT mode"))}"
    fi
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
t_forward6() { [ "$(sysctl -n net.ipv6.conf.all.forwarding)" = 1 ]; }
# warp has no IPv6 route in the main table (Table = off), and IPv6 won't use
# an interface without one, so route WARP's own address through RT_TABLE for
# the length of the check.
t_warp6_on() {
    local src out prio=$(( RULE_PRIO - 1 ))
    src=$(ip -6 addr show dev "$WARP_IF" scope global | awk '/inet6/ { sub(/\/.*/, "", $2); print $2; exit }')
    [ -n "$src" ] || return 1
    ip -6 rule add from "$src" lookup "$RT_TABLE" priority "$prio" 2>/dev/null || true
    out=$(curl -6 -fsS --max-time 10 --interface "$src" "https://[2606:4700:4700::1111]/cdn-cgi/trace" 2>/dev/null || true)
    ip -6 rule del from "$src" lookup "$RT_TABLE" priority "$prio" 2>/dev/null || true
    [[ $out == *$'\nwarp=on'* || $out == *$'\nwarp=plus'* ]]
}
# ip rule / table checks for family $1 ("ip" or "ip -6").
t_ip_rule() {
    local net="$WG_SUBNET"
    if [ "$1" = "ip -6" ]; then net="$WG_SUBNET6"; fi
    [ "$($1 rule show | grep -cF "from $net lookup $RT_TABLE")" -eq 1 ]
}
t_table() {
    local r net="$WG_SUBNET"
    if [ "$1" = "ip -6" ]; then net="$WG_SUBNET6"; fi
    r=$($1 route show table "$RT_TABLE" 2>/dev/null || true)
    grep -qE "^default dev $WARP_IF( |$)" <<<"$r" \
        && grep -qE "^blackhole default( |$)" <<<"$r" \
        && grep -qE "^${net//./\\.} dev $WG_IF( |$)" <<<"$r"
}
# Exactly one copy of a firewall rule ("command|table|chain|rule"), via -S.
t_ipt_once() {
    local x t c r re
    IFS='|' read -r x t c r <<<"$1"
    re=$(sed -E 's/\./\\./g; s/-p (tcp|udp) /-p \1 (-m \1 )?/' <<<"-A $c $r")
    [ "$($x -t "$t" -S "$c" 2>/dev/null | grep -cE "^${re}\$")" -eq 1 ]
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
        if [ "$IPV6" -eq 1 ]; then check "$WARP_IF: IPv6 through $WARP_IF shows warp=on" t_warp6_on; fi
        check "$WARP_IF: handshake within the last 3 minutes" t_warp_handshake
        check "server's own traffic is not tunneled (plain curl shows warp=off)" t_untunneled
    fi
    check "default route unchanged since install ($(state_get DEFAULT_ROUTE))" t_default_route
    [ "$1" != warp ] || return 0
    if [ ! -f "$WG_CONF" ]; then
        t_result SKIP "$WG_IF checks (the WireGuard server isn't set up yet)"
        return 0
    fi
    check "$WG_IF listening on udp/$WG_PORT" t_listen
    check "net.ipv4.ip_forward = 1" t_forward
    if [ "$IPV6" -eq 1 ]; then check "net.ipv6.conf.all.forwarding = 1" t_forward6; fi
    if [ "$EXIT" = warp ]; then
        local f net
        while IFS= read -r f; do
            net="$WG_SUBNET"
            if [ "$f" = "ip -6" ]; then net="$WG_SUBNET6"; fi
            check "$f rule: from $net lookup $RT_TABLE (exactly once)" t_ip_rule "$f"
            check "$f table $RT_TABLE: default via $WARP_IF, blackhole fallback, $net via $WG_IF" t_table "$f"
        done < <(ip_families)
    fi
    while IFS= read -r rule; do
        check "exactly once: ${rule//|/ }" t_ipt_once "$rule"
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
    require_mode
    log "Tests ($EXIT mode)"
    run_tests all
    report_tests
}

# --- backup ---

# Keep the KEEP_ARCHIVES newest backup archives in <dir>.
prune_archives() {
    local a archives=() i
    for a in "$1"/wireguard-tools.*.tar.gz; do
        if [ -f "$a" ]; then archives+=("$a"); fi
    done
    # Names end in a timestamp, so the glob's order is oldest first.
    for (( i = 0; i < ${#archives[@]} - KEEP_ARCHIVES; i++ )); do
        run rm -f "${archives[$i]}"
        info "pruned old archive: ${archives[$i]}"
    done
}

# Archive keys, configs, clients and state (mode 600; it holds private keys),
# then prune old archives and change copies.
cmd_backup() {
    local dir="${1:-$BACKUP_DIR}" out paths=() p
    for p in "$WG_DIR" "$CLIENT_DIR" "$SYSCTL_FILE" "$DROPIN_DIR"; do
        if [ -e "$p" ]; then paths+=("$p"); fi
    done
    [ ${#paths[@]} -gt 0 ] || die "nothing to back up"
    take_lock   # don't archive mid-change
    out="$dir/wireguard-tools.$(hostname -s).$(date +%Y%m%d-%H%M%S).tar.gz"
    step "Back up ${paths[*]} to $out" "rm $out"
    run install -d -m 700 "$dir"
    run tar -czf "$out" -P "${paths[@]}"
    run chmod 600 "$out"
    prune_archives "$dir"
    prune_all_changes
    warn "the archive contains private keys; keep it somewhere safe"
    info "restore on a fresh server: $PROG restore <archive>"
    info "fetch it with: scp root@$(state_get ENDPOINT | tr -d '[]'):$out ."
}

# --- restore ---

# Set this server up from a backup archive. Only keys, clients, the WARP
# account and settings come across; the old server's baseline (default route,
# interface, ip_forward), firewall record and file-backup records are dropped,
# and an IP endpoint is re-detected. Then the saved mode's setup runs.
cmd_restore() {
    local archive="${1:-}" members mode ep k
    [ -n "$archive" ] || die "usage: $PROG restore <archive.tar.gz>"
    [ -f "$archive" ] || die "no such file: $archive"
    archive=$(readlink -f "$archive")
    members=$(tar -tzf "$archive" 2>/dev/null) || die "$archive is not a readable .tar.gz"
    grep -qx "$STATE" <<<"$members" || die "$archive is not a wireguard-tools backup (no $STATE inside)"
    # Restoring over a live setup would orphan its rules and lose its rollback record.
    if [ -n "$SAVED_EXIT" ]; then
        die "this server is already set up ($SAVED_EXIT); run '$PROG rollback' (or 'rollback --purge') first"
    fi
    mode=$(tar -xzOf "$archive" -P "$STATE" | sed -n 's/^EXIT=//p' | tail -n 1)
    case "$mode" in direct|warp) ;; *) die "backup has no usable mode (EXIT=$mode)" ;; esac

    take_lock
    local what="keys, clients and settings"
    if [ "$mode" = warp ]; then what="keys, clients, settings and WARP account"; fi
    step "Restore $what from $archive ($mode mode)" \
        "$PROG rollback --purge"
    confirm "Restore from this backup?"
    local paths=("$WG_DIR") keep_map=""
    if grep -q "^$CLIENT_DIR/" <<<"$members"; then paths+=("$CLIENT_DIR"); fi
    # This server's own backup record (left by an earlier rollback) lists its
    # original files; keep it over the archive's, which describes the old server.
    if [ -f "$BACKUP_MAP" ]; then keep_map="$TMPD/backups"; cp -a "$BACKUP_MAP" "$keep_map"; fi
    run tar -xzpf "$archive" -P "${paths[@]}"
    if [ "$DRY_RUN" -eq 1 ]; then
        info "[dry-run] would then run: $PROG $mode"
        return 0
    fi
    for k in DEFAULT_ROUTE PUB_IF ORIG_IP_FORWARD ORIG_IP6_FORWARD ORIG_ACCEPT_RA UFW; do state_unset "$k"; done
    ep=$(state_get ENDPOINT)
    if [[ $ep =~ ^\[?[0-9a-fA-F:.]+\]?$ ]]; then state_unset ENDPOINT; fi
    if [ -n "$keep_map" ]; then install -m 600 "$keep_map" "$BACKUP_MAP"; else rm -f "$BACKUP_MAP"; fi
    # Everything restored is new to this server: record it so, or the first
    # change would record the restored copy as the file's "original".
    local m
    while IFS= read -r m; do
        case "$m" in */|*.bak|"$BACKUP_MAP") continue ;; esac
        if [ -f "$m" ] && [ -z "$(backup_orig "$m")" ]; then printf '%s\tNEW\n' "$m" >>"$BACKUP_MAP"; fi
    done < <(grep -E "^($WG_DIR|$CLIENT_DIR)/" <<<"$members")
    chmod 600 "$BACKUP_MAP" 2>/dev/null || true
    info "restored; clients: $(db_entries | awk '{ printf "%s ", $1 }')"
    if [ "$mode" = warp ]; then
        warn "the WARP account is now on this server too; don't keep running it on the old one"
    fi
    flock -u 9
    local flags=()
    if [ "$YES" -eq 1 ]; then flags+=(--yes); fi
    log "Setting up ($mode)"
    info "if setup stops, fix the cause and run '$PROG $mode' (the restored files stay in place)"
    # exec skips the EXIT trap: remove our temp dir and hand back the caller's
    # TMPDIR, or the new process's mktemp would use the deleted dir.
    rm -rf "$TMPD"
    if [ -n "$CALLER_TMPDIR" ]; then export TMPDIR="$CALLER_TMPDIR"; else unset TMPDIR; fi
    exec "$SELF" "${flags[@]+"${flags[@]}"}" "$mode"
}

# --- rollback ---

# Restore a file's original backup; files we created are removed if <rm_new> is 1.
restore_original() {
    local f="$1" rm_new="$2" orig
    orig=$(backup_orig "$f")
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
    local orig_fwd orig_fwd6 orig_ra f out pub
    orig_fwd=$(state_get ORIG_IP_FORWARD)
    orig_fwd6=$(state_get ORIG_IP6_FORWARD)
    orig_ra=$(state_get ORIG_ACCEPT_RA)
    pub=$(pub_if)
    out=$(out_if)
    local ifs="$WG_IF"
    if [ "$EXIT" = warp ]; then ifs="$WG_IF and $WARP_IF"; fi
    step "Roll back ($EXIT mode): stop $ifs, remove routing/NAT/firewall rules, restore original files" \
        "$PROG $EXIT"
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

    # Our own drop-in's .bak copies would keep its directory from being removed.
    # shellcheck disable=SC2086  # glob on purpose
    if [ "$(backup_orig "$DROPIN")" = NEW ]; then run rm -f $DROPIN.*.bak; fi
    restore_original "$DROPIN" 1
    if [ -d "$DROPIN_DIR" ]; then run rmdir --ignore-fail-on-non-empty "$DROPIN_DIR"; fi
    run systemctl daemon-reload
    restore_original "$SYSCTL_FILE" 1
    if [ -n "$orig_fwd" ]; then run sysctl -q -w "net.ipv4.ip_forward=$orig_fwd"; fi
    if [ -n "$orig_fwd6" ]; then run sysctl -q -w "net.ipv6.conf.all.forwarding=$orig_fwd6"; fi
    if [ -n "$orig_ra" ]; then run sysctl -q -w "net.ipv6.conf.$pub.accept_ra=$orig_ra" || true; fi
    restore_original "$WG_CONF" "$PURGE"
    restore_original "$WARP_CONF" "$PURGE"

    if [ "$PURGE" -eq 1 ]; then
        for f in "$WG_CONF" "$WARP_CONF" "$SERVER_KEY" "$SERVER_PUB" "$CLIENT_DB" "$SYSCTL_FILE"; do
            # shellcheck disable=SC2086  # glob on purpose: copies left by older versions
            run rm -f $f.*.bak
        done
        # Originals were restored above; the change copies hold old keys.
        run rm -rf "$CHANGE_DIR"
        # Configs carrying our marker are ours (a restored original never does).
        for f in "$WG_CONF" "$WARP_CONF"; do
            if [ -f "$f" ] && grep -qF "$MARKER" "$f"; then run rm -f "$f"; fi
        done
        run rm -f "$SERVER_KEY" "$SERVER_PUB" "$CLIENT_DB" "$STATE" "$BACKUP_MAP"
        run rm -rf "$WGCF_DIR" "$CLIENT_DIR" "$PSK_DIR"
        info "left installed: packages and $WGCF_BIN"
    else
        # Free the mode (and its MTU default) so the next setup can pick either.
        state_unset EXIT
        state_unset WG_MTU
        local kept="keys, clients ($CLIENT_DB, $CLIENT_DIR)"
        if [ -d "$WGCF_DIR" ]; then kept="$kept, WARP account ($WGCF_DIR)"; fi
        info "kept: $kept (rollback --purge removes them)"
    fi
    log "Rollback done; the main routing table was never changed"
}

# --- main ---

# The setup commands choose the mode; everything else uses the saved one.
command_mode() {
    case "$1" in
        direct|warp) echo "$1" ;;
    esac
}

main() {
    init "$@"
    set -- "${ARGS[@]}"
    local cmd="$1"
    shift
    case "$cmd" in
        direct)   cmd_direct ;;
        warp)     cmd_warp ;;
        server)   die "'server' is now '$PROG direct' or '$PROG warp'" ;;
        test)     cmd_test ;;
        status)   cmd_status ;;
        backup)   cmd_backup "$@" ;;
        restore)  cmd_restore "$@" ;;
        rollback) cmd_rollback ;;
        *) die "unknown command: $cmd (see: $PROG help)" ;;
    esac
}

main "$@"
