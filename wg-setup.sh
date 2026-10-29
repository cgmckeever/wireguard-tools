#!/usr/bin/env bash
# wg-setup.sh — Set up this Debian/Ubuntu server as a WireGuard VPN server,
# with Cloudflare WARP as a switch: `warp on` sends client traffic out through
# WARP, `warp off` out this server's own IP. Clients are managed with
# wg-client.sh. See README.md.

set -Eeuo pipefail

PROG=$(basename "$0")
SELF=$(readlink -f "${BASH_SOURCE[0]}")
# shellcheck source=lib/common.sh
. "$(dirname "$SELF")/lib/common.sh"

# Print help.
usage() {
    cat <<EOF
Usage: $PROG [options] <command>

Sets up this server as a WireGuard VPN server whose clients exit either
through Cloudflare WARP or via this server's own IP, switchable at any time:

  $PROG setup      Install or update everything (WARP on by default)
  $PROG warp off   Clients exit via this server's IP
  $PROG warp on    Clients exit through WARP (blocked, not leaked, if WARP drops)

Then add clients with wg-client.sh. Re-run setup to apply changes (e.g. --port).

Commands:
  setup       Install packages and wgcf, register a free WARP account, bring up
              $WARP_IF (Table = off) and test it, then create $WG_IF: server
              keys, NAT for both ways out, firewall, forwarding, systemd units.
              If WARP can't be set up on a first install, finishes with WARP off
  warp [on|off]
              Switch where clients exit (live: $WG_IF isn't restarted, clients
              stay connected); no argument shows the current setting
  test        All verification checks; exits non-zero on any FAIL
  status      Settings (and whether wg0 runs them), interfaces, clients, routes, rules
  backup [dir]  Archive keys, configs and clients (default dir $BACKUP_DIR)
  restore <archive>
              Set this server up from a backup: same keys, clients, WARP
              account and settings, then runs setup
  rollback    Stop/disable the interfaces, remove all rules, restore original
              files; keeps keys and clients unless --purge
  help        This text

Options:
  --dry-run             Show what would change; change nothing
  --yes, -y             Skip the per-step prompts (setup still asks to confirm its settings)
  --force               Regenerate existing keys, reinstall wgcf, regenerate the
                        WARP profile, or take over configs not made by
                        wireguard-tools
  --purge               With rollback: also delete keys, clients, WARP account

Config (flag / environment variable; saved by setup for later runs):
  --name         SERVER_NAME          $SERVER_NAME  (client configs: <name>.<client>.conf)
  --subnet       WG_SUBNET            ${SAVED[WG_SUBNET]:-<random 10.[1-10].[1-10].0/24 at setup>}  (setup asks)
  --subnet6      WG_SUBNET6           ${WG_SUBNET6:-<random fd::/64 at setup>}  (client IPv6; "off" disables)
  --port         WG_PORT              $WG_PORT
  --mtu          WG_MTU               $WG_MTU
  --warp-mtu     WARP_MTU             $WARP_MTU
  --dns          WG_DNS               $WG_DNS  (DNS server given to clients)
  --endpoint     ENDPOINT             ${ENDPOINT:-<auto-detected public IPv4>}
  --wg-if        WG_IF                $WG_IF
  --warp-if      WARP_IF              $WARP_IF
  --table        RT_TABLE             $RT_TABLE  (WARP policy-routing table)
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
# Returns 1 if it can't be fetched (setup can carry on without WARP); a
# checksum mismatch is fatal.
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
    json=$(curl -fsSL --max-time 30 "$api") || { warn "cannot reach GitHub releases API ($api)"; return 1; }
    url=$(grep -oE '"browser_download_url": *"[^"]+"' <<<"$json" | grep -oE 'https://[^"]+' | grep -E "_linux_${ARCH}\$" | head -n 1 || true)
    [ -n "$url" ] || { warn "no linux_$ARCH asset in wgcf release $WGCF_VERSION"; return 1; }
    sums_url=$(grep -oE '"browser_download_url": *"[^"]+"' <<<"$json" | grep -oE 'https://[^"]+' | grep -iE 'checksums|sha256' | head -n 1 || true)
    curl -fsSL --max-time 120 -o "$TMPD/wgcf" "$url" || { warn "download failed: $url"; return 1; }

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
    "$TMPD/wgcf" --help >/dev/null 2>&1 || { warn "downloaded wgcf does not run"; return 1; }
    install -m 755 "$TMPD/wgcf" "$WGCF_BIN"
    info "installed $(basename "$url")"
}

# Register a WARP account and generate the WireGuard profile (once).
# Returns 1 if WARP refuses (setup can carry on without WARP).
register_warp() {
    local log="$WGCF_DIR/wgcf.log"
    if [ -f "$WGCF_DIR/wgcf-account.toml" ]; then
        info "WARP account exists ($WGCF_DIR/wgcf-account.toml); not re-registering"
    else
        step "Register a free WARP account (accepts Cloudflare's WARP terms)" "rm -rf $WGCF_DIR"
        if [ "$DRY_RUN" -eq 0 ]; then
            if ! ( cd "$WGCF_DIR" && umask 077 && "$WGCF_BIN" register --accept-tos ) >"$log" 2>&1; then
                warn "wgcf register failed; see $log. WARP may refuse registrations from some server IPs:
       retry later ('$PROG warp on'), or register on another machine and copy
       wgcf-account.toml and wgcf-profile.conf into $WGCF_DIR/ (see README)"
                return 1
            fi
        fi
    fi
    if [ -f "$WGCF_DIR/wgcf-profile.conf" ] && [ "$FORCE" -eq 0 ]; then
        info "WARP profile exists"
        return 0
    fi
    step "Generate WARP WireGuard profile" "rm $WGCF_DIR/wgcf-profile.conf"
    if [ "$DRY_RUN" -eq 0 ]; then
        if ! ( cd "$WGCF_DIR" && umask 077 && "$WGCF_BIN" generate ) >>"$log" 2>&1; then
            warn "wgcf generate failed; see $log"
            return 1
        fi
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

# Install, register and bring up the WARP tunnel, then test it. Returns 1 if
# WARP isn't usable (nothing on the client side depends on it yet).
provision_warp() {
    log "WARP tunnel"
    if [ "$DRY_RUN" -eq 0 ]; then install -d -m 700 "$WGCF_DIR"; fi
    install_wgcf || return 1
    register_warp || return 1
    write_warp_conf
    start_unit "$WARP_IF" "$WARP_CHANGED"
    guard_default_route
    if [ "$DRY_RUN" -eq 1 ]; then
        log "Dry run: skipping WARP tests"
        return 0
    fi
    log "WARP tests"
    T_PASS=0 T_FAIL=0 T_WARN=0
    run_tests warp
    if [ "$T_FAIL" -gt 0 ]; then
        warn "WARP failed $T_FAIL check(s); see '$PROG status'"
        return 1
    fi
    info "WARP is working ($T_PASS checks passed)"
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
        confirm "Enable forwarding?" y   # clients get nowhere without it
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

# Start wg0 after warp at boot (Wants, not Requires: with WARP off, wg0 must
# come up even if warp can't; with it on, RT_TABLE's blackhole fails closed).
write_dropin() {
    local content
    content=$(render_template wg-quick-after-warp.conf.tmpl WG_IF="$WG_IF" WARP_IF="$WARP_IF")
    write_file "$DROPIN" 644 <<<"$content"
    if [ "$WROTE" -eq 1 ]; then run systemctl daemon-reload; fi
}

# Install the WARP watchdog (script + service + timer) and keep its timer on.
write_watchdog() {
    local changed=0 content
    content=$(render_template warp-watchdog.sh.tmpl WARP_IF="$WARP_IF" STALE="$WATCHDOG_STALE")
    write_file "$WATCHDOG_BIN" 755 <<<"$content"
    if [ "$WROTE" -eq 1 ]; then changed=1; fi
    content=$(render_template warp-watchdog.service.tmpl WARP_IF="$WARP_IF" SCRIPT="$WATCHDOG_BIN")
    write_file "$WATCHDOG_SERVICE" 644 <<<"$content"
    if [ "$WROTE" -eq 1 ]; then changed=1; fi
    content=$(render_template warp-watchdog.timer.tmpl)
    write_file "$WATCHDOG_TIMER" 644 <<<"$content"
    if [ "$WROTE" -eq 1 ]; then changed=1; fi
    if [ "$changed" -eq 1 ]; then run systemctl daemon-reload; fi
    if ! systemctl is-active -q "$WATCHDOG.timer" || ! systemctl is-enabled -q "$WATCHDOG.timer" 2>/dev/null; then
        step "Start the WARP watchdog ($WATCHDOG.timer: new source port for $WARP_IF after ${WATCHDOG_STALE}s without a handshake)" \
            "systemctl disable --now $WATCHDOG.timer"
        run systemctl enable --now "$WATCHDOG.timer"
    fi
}

# Keep every client config named <server>.<client>.conf (renaming ones from an
# older server name, or from before server names), pointed at the current
# endpoint and port, DNS and MTU, and with its current addresses (IPv6 on or
# off) and allowed IPs from clients.db. Keys are
# unchanged, so only the configs need re-sending.
sync_client_configs() {
    local want="$ENDPOINT:$WG_PORT" conf src old name ip _ allowed addr stale=()
    # Configs are found via clients.db, so editing the template can't hide one.
    while read -r name ip _ allowed; do
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
        allowed="${allowed//,/, }"
        grep -qxF "Endpoint = $want" "$src" && grep -qxF "Address = $addr" "$src" \
            && grep -qxF "DNS = $WG_DNS" "$src" && grep -qxF "MTU = $WG_MTU" "$src" \
            && { [ -z "$allowed" ] || grep -qxF "AllowedIPs = $allowed" "$src"; } && continue
        local edits=(-e "s|^Endpoint = .*|Endpoint = $want|" -e "s|^Address = .*|Address = $addr|"
                     -e "s|^DNS = .*|DNS = $WG_DNS|" -e "s|^MTU = .*|MTU = $WG_MTU|")
        if [ -n "$allowed" ]; then edits+=(-e "s|^AllowedIPs = .*|AllowedIPs = $allowed|"); fi
        write_file "$conf" 600 < <(sed "${edits[@]}" "$src")
        [[ " ${stale[*]} " == *" $name "* ]] || stale+=("$name")
    done < <(db_entries)
    if [ ${#stale[@]} -gt 0 ]; then
        warn "client configs changed (now $SERVER_NAME.<client>.conf at $want); re-send them (wg-client.sh share/show --qr): ${stale[*]}"
    fi
}

# ufw (if active): allow the WireGuard port and wg0 -> both ways out.
open_firewall() {
    local st out
    st=$(ufw status 2>/dev/null || true)
    if [[ $st == *"Status: active"* ]]; then
        step "ufw: allow $WG_PORT/udp and routing $WG_IF -> $(exit_ifs | paste -sd' ' -)" \
            "ufw delete allow $WG_PORT/udp; ufw route delete allow in on $WG_IF out on <each>"
        run ufw allow "$WG_PORT/udp" comment wireguard-tools
        while IFS= read -r out; do
            run ufw route allow in on "$WG_IF" out on "$out" comment wireguard-tools
        done < <(exit_ifs)
        if [ "$(state_get UFW)" != 1 ]; then state_set UFW 1; fi
    else
        info "ufw not active; $WG_IF's PostUp opens udp/$WG_PORT in iptables INPUT"
    fi
}

# Refuse to take over a wg0.conf we didn't create. Must run before
# save_config records the new values.
check_server_change() {
    ensure_managed "$WG_CONF"
}

# A new --subnet with clients present: move each client to the same host
# number in the new subnet (10.8.0.5 -> 192.168.250.5), keeping its keys, and
# rewrite allowed-IPs entries that name the old subnet. sync_client_configs
# then updates their configs (re-send them; keys are unchanged).
renumber_clients() {
    [ -n "$SAVED_SUBNET" ] && [ "$SAVED_SUBNET" != "$WG_SUBNET" ] || return 0
    [ -n "$(db_entries)" ] || return 0
    local old_net old_pfx old_int name ip pub allowed n new moves=() lines=()
    old_net="${SAVED_SUBNET%/*}"
    old_pfx="${SAVED_SUBNET#*/}"
    old_int=$(( $(ip2int "$old_net") & ((0xFFFFFFFF << (32 - old_pfx)) & 0xFFFFFFFF) ))
    while read -r name ip pub allowed; do
        n=$(( NET_INT + $(ip2int "$ip") - old_int ))
        [ "$n" -ge "$FIRST_HOST" ] && [ "$n" -le "$LAST_HOST" ] \
            || die "client '$name' ($ip) has no matching address in $WG_SUBNET; use a subnet at least as large as $SAVED_SUBNET"
        new=$(int2ip "$n")
        allowed=$(tr ',' '\n' <<<"$allowed" | sed "s|^$SAVED_SUBNET\$|$WG_SUBNET|" | paste -sd, -)
        moves+=("$name $ip -> $new")
        lines+=("$name $new $pub $allowed")
    done < <(db_entries)
    step "Move ${#moves[@]} client(s) from $SAVED_SUBNET to $WG_SUBNET (same host numbers and keys; their configs change, re-send them)" \
        "$PROG setup --subnet $SAVED_SUBNET"
    printf '    %s\n' "${moves[@]}"
    confirm "Move the clients to $WG_SUBNET?"
    write_file "$CLIENT_DB" 600 < <(
        echo "# wireguard-tools clients: name ip public-key allowed-ips (no private keys)"
        printf '%s\n' "${lines[@]}"
    )
}

# wg0's config is about to change: stop wg0 with its current config first, so
# its PostDown removes exactly the rules its PostUp added (old subnet, old
# port); restarting after the rewrite would run the new PostDown instead and
# leave the old rules behind. start_unit then brings it up with the new one.
WG_STOPPED=0
stop_before_rewrite() {
    local unit="wg-quick@$WG_IF"
    if_exists "$WG_IF" && [ -f "$WG_CONF" ] || return 0
    ! cmp -s <(render_wg_conf) "$WG_CONF" || return 0
    step "Restart $WG_IF for its new config (clients reconnect in a few seconds)" "restore the .bak in $CHANGE_DIR, then systemctl restart $unit"
    confirm "Restart $WG_IF now?"
    if systemctl is-active -q "$unit"; then run systemctl stop "$unit"; else run wg-quick down "$WG_IF"; fi
    WG_STOPPED=1
}

# The server must be set up; after rollback no WARP setting is saved.
require_setup() {
    [ -n "$SAVED_WARP" ] || die "not set up; run '$PROG setup'"
}

# Install or update everything: WARP tunnel, then wg0 with the saved WARP
# setting (on for a new install). WARP failing on a first install leaves it
# off; on an existing install with WARP on it stays on (fail closed: clients
# are blocked, not leaked) until fixed or switched off.
# First setup without --subnet: ask for the client subnet, Enter keeps the
# default. --yes, --dry-run and no terminal take the default.
ask_subnet() {
    [ "$SUBNET_DEFAULTED" -eq 1 ] || return 0
    if [ "$DRY_RUN" -eq 1 ] || [ "$YES" -eq 1 ] || ! have_tty; then return 0; fi
    local def="$WG_SUBNET" reply err
    while :; do
        read -r -p "    Client subnet [$def]: " reply </dev/tty || true
        WG_SUBNET="${reply:-$def}"
        if err=$( (finalize_config) 2>&1 ); then break; fi
        printf '    %s\n' "${err#ERROR: }"
    done
    finalize_config
    if [ -n "$reply" ]; then SUBNET_ASKED=1; fi
}

# Where a setting's value comes from.
setting_src() {
    local k="$1"
    if [ -n "${GIVEN_FLAG[$k]:-}" ]; then echo flag
    elif [ -n "${GIVEN_ENV[$k]:-}" ]; then echo env
    elif [ "$k" = WG_SUBNET ] && [ "$SUBNET_ASKED" -eq 1 ]; then echo entered
    elif [ -n "${SAVED[$k]}" ]; then echo saved
    elif [ "$k" = WG_SUBNET ] || [ "$k" = WG_SUBNET6 ]; then echo "random (new)"
    elif [ "$k" = ENDPOINT ]; then echo detected
    else echo default
    fi
}

# What wg0/warp are actually running for a setting, if it can be read.
running_value() {
    case "$1" in
        WG_PORT)    wg show "$WG_IF" listen-port 2>/dev/null ;;
        WG_SUBNET)  ip -4 -o addr show dev "$WG_IF" 2>/dev/null | awk '{ print $4; exit }' ;;
        WG_SUBNET6) ip -6 -o addr show dev "$WG_IF" scope global 2>/dev/null | awk '{ print $4; exit }' ;;
        WG_MTU)     cat "/sys/class/net/$WG_IF/mtu" 2>/dev/null ;;
        WARP_MTU)   cat "/sys/class/net/$WARP_IF/mtu" 2>/dev/null ;;
        WARP)       if [[ $(ip rule show) == *"from $WG_SUBNET lookup $RT_TABLE"* ]]; then echo on; else echo off; fi ;;
    esac
}

# The running value a setting should have (none for settings that only live
# in client configs: name, endpoint, DNS, allowed IPs).
expected_running() {
    case "$1" in
        WG_SUBNET)  echo "$SERVER_IP/$PFX" ;;
        WG_SUBNET6) if [ "$IPV6" -eq 1 ]; then echo "$P6::1/64"; fi ;;
        WG_PORT|WG_MTU|WARP_MTU|WARP) echo "${!1}" ;;
    esac
}

# The settings table: value and where it came from, plus with <running>=1
# what wg0/warp run now (pending when it differs: saved but not applied yet,
# re-run setup). Fills SETTING_CHANGES with
# differences from the saved values, and SETTING_CFG=1 if any changes client
# configs; RUNNING_PENDING counts pending settings.
settings_table() {
    local running="${1:-0}" k label def row now want extra
    SETTING_CHANGES=() SETTING_CFG=0 RUNNING_PENDING=0
    # key|label|default (a row with a default is only shown when it differs)
    for row in "SERVER_NAME|name|" "ENDPOINT|endpoint|" "WG_PORT|port|" "WG_SUBNET|subnet|" \
               "WG_SUBNET6|subnet6|" "WARP|WARP|" "WG_DNS|DNS|" "CLIENT_ALLOWED_IPS|allowed IPs|" \
               "WG_MTU|MTU|" "WARP_MTU|WARP MTU|1280" "WG_IF|interface|wg0" "WARP_IF|WARP interface|warp" \
               "RT_TABLE|table|100"; do
        IFS='|' read -r k label def <<<"$row"
        if [ -n "$def" ] && [ "${!k}" = "$def" ] && [ "${SAVED[$k]:-$def}" = "$def" ]; then continue; fi
        extra=""
        if [ "$running" -eq 1 ]; then
            now=$(running_value "$k")
            want=$(expected_running "$k")
            if [ -n "$want" ] || [ -n "$now" ]; then
                extra="running: ${now:-none}"
                if [ "$now" != "$want" ]; then extra="$extra  (pending)"; RUNNING_PENDING=$(( RUNNING_PENDING + 1 )); fi
            fi
        fi
        printf '    %-15s %-24s %-8s %s\n' "$label" "${!k:-<not detected>}" "$(setting_src "$k")" "$extra" | sed 's/ *$//'
        if [ -n "${SAVED[$k]}" ] && [ "${SAVED[$k]}" != "${!k}" ]; then
            SETTING_CHANGES+=("$(printf '%-15s %s -> %s' "$label" "${SAVED[$k]}" "${!k}")")
            case "$k" in SERVER_NAME|ENDPOINT|WG_PORT|WG_SUBNET|WG_SUBNET6|WG_DNS|WG_MTU) SETTING_CFG=1 ;; esac
        fi
    done
}

# Before anything changes: show the settings this run uses, where each comes
# from and what differs from the last setup, then ask to continue (even with
# --yes; --dry-run doesn't ask).
review_settings() {
    local fresh=0 k
    if [ -z "${SAVED[WG_SUBNET]}" ]; then fresh=1; fi
    if [ -z "$ENDPOINT" ]; then ENDPOINT="${SAVED[ENDPOINT]}"; fi
    if [ -z "$ENDPOINT" ]; then ENDPOINT=$(trace_field ip -4 2>/dev/null || true); fi
    if [ "$fresh" -eq 1 ]; then log "Settings (first setup)"; else log "Settings (re-run; saved in $STATE)"; fi
    settings_table 0
    if [ "$fresh" -eq 1 ]; then
        info "Nothing is set up yet; these are saved for re-runs and wg-client.sh."
    elif [ ${#SETTING_CHANGES[@]} -eq 0 ]; then
        info "No changes to settings since the last setup."
    else
        info "Changes since the last setup:"
        for k in "${SETTING_CHANGES[@]}"; do info "  $k"; done
        if [ "$SETTING_CFG" -eq 1 ] && [ -n "$(db_entries)" ]; then
            info "Client configs will be rewritten; re-send them."
        fi
    fi
    STEP="review settings"
    if [ "$DRY_RUN" -eq 1 ]; then return 0; fi
    # --yes skips the step prompts, not this one; it only lets a run with no
    # terminal (a script) carry on past it.
    if ! have_tty; then
        [ "$YES" -eq 1 ] || die "no terminal to confirm the settings; re-run with --yes"
        return 0
    fi
    local reply=""
    read -r -p "    Continue? [Y/n] " reply </dev/tty || true
    [[ ${reply:-y} =~ ^[Yy] ]] || die "aborted at: review settings (nothing changed)"
}

cmd_setup() {
    take_lock
    ask_subnet
    ensure_subnet6
    review_settings
    # Refuse a breaking change before saving config or touching anything.
    check_server_change
    renumber_clients
    save_config
    state_unset EXIT   # older versions' mode key; WARP replaces it
    record_baseline
    install_packages
    local warp_ok=1
    provision_warp || warp_ok=0
    if [ "$warp_ok" -eq 0 ]; then
        if [ -z "$SAVED_WARP" ] && [ "$WARP" = on ]; then
            warn "WARP isn't usable here; finishing with WARP off (clients exit via this server's IP). Fix it, then: $PROG warp on"
            WARP=off
            save_config
        elif [ "$WARP" = on ]; then
            warn "WARP is on but not working: clients are blocked until it is (or: $PROG warp off)"
        fi
    fi
    log "WireGuard server (WARP $WARP: client traffic leaves via $(out_if))"
    ensure_server_keys
    resolve_endpoint 1
    sync_client_configs
    write_sysctl
    write_dropin
    stop_before_rewrite
    write_wg_conf
    local changed="$WROTE"
    if [ "$KEYS_CHANGED" -eq 1 ]; then changed=1; fi
    open_firewall
    CLEANUP_ON_ERR=1
    if [ "$WG_STOPPED" -eq 1 ] && [ "$DRY_RUN" -eq 1 ]; then
        info "[dry-run] would then start wg-quick@$WG_IF with the new config"
    else
        start_unit "$WG_IF" "$changed"
    fi
    CLEANUP_ON_ERR=0
    guard_default_route
    write_watchdog
    echo ""
    info "WARP is $WARP; switch with: $PROG warp on|off"
    info "REMINDER: open UDP $WG_PORT in your VPS provider's firewall / security group."
    info "Next: wg-client.sh add <name>"
}

# Show or switch WARP. Switching rewrites wg0.conf (for the next start) and
# adds/removes the policy rule live; wg0 isn't restarted.
cmd_warp() {
    local want="${1:-}"
    require_setup
    case "$want" in
        "")
            info "WARP is $WARP: client traffic leaves via $(out_if)"
            return 0
            ;;
        on|off) ;;
        *) die "usage: $PROG warp [on|off]" ;;
    esac
    take_lock
    if [ "$want" = on ]; then
        provision_warp || die "WARP isn't working; leaving it off (see '$PROG status')"
    fi
    if [ "$WARP" = "$want" ] && [ -f "$WG_CONF" ] && grep -qF "(WARP $want)" "$WG_CONF"; then
        info "WARP is already $want (client traffic leaves via $(out_if))"
        if if_exists "$WG_IF"; then run_hooks warp "$want" >/dev/null; fi
        return 0
    fi
    WARP="$want"
    step "WARP $want: client traffic leaves via $(out_if)" "$PROG warp $( [ "$want" = on ] && echo off || echo on)"
    save_config
    write_wg_conf
    if if_exists "$WG_IF"; then
        run_hooks warp "$want"
        info "applied live; clients stay connected (open connections reset once)"
    else
        info "$WG_IF is down; applies when it starts"
    fi
}

# --- status ---

cmd_status() {
    local ifc unit active enabled x t c r f
    require_setup
    if [ -z "$ENDPOINT" ]; then ENDPOINT="${SAVED[ENDPOINT]}"; fi
    log "Settings (saved in $STATE)"
    settings_table 1
    if [ "$RUNNING_PENDING" -gt 0 ]; then
        warn "$RUNNING_PENDING saved setting(s) pending (not running yet); apply with: $PROG setup"
    fi
    log "WARP $WARP (client traffic leaves via $(out_if))"
    log "Interfaces"
    for ifc in "$WARP_IF" "$WG_IF"; do
        unit="wg-quick@$ifc"
        active=$(systemctl is-active "$unit" 2>/dev/null || true)
        enabled=$(systemctl is-enabled "$unit" 2>/dev/null || true)
        if if_exists "$ifc"; then
            info "$(ip -br addr show dev "$ifc")  [$active, ${enabled:-disabled}]"
        else
            info "$ifc: absent  [${enabled:-disabled}]"
        fi
    done
    local wd_last
    wd_last=$(journalctl -t "$WATCHDOG" -n 1 -o short-iso --no-pager -q 2>/dev/null | cut -c1-25 || true)
    info "watchdog: $WATCHDOG.timer $(systemctl is-active "$WATCHDOG.timer" 2>/dev/null || true); last port change: ${wd_last:-never}"
    log "WARP handshake"
    if if_exists "$WARP_IF"; then
        info "$WARP_IF: $(ago "$(wg show "$WARP_IF" latest-handshakes | awk '$2 > m { m = $2 } END { print m + 0 }')")"
    fi
    local -A hs=() rx=() tx=()
    local k ts b name ip pub allowed seen n=0
    if if_exists "$WG_IF"; then
        while read -r k ts; do hs[$k]="$ts"; done < <(wg show "$WG_IF" latest-handshakes)
        while read -r k ts b; do rx[$k]="$ts"; tx[$k]="$b"; done < <(wg show "$WG_IF" transfer)
    fi
    log "Clients"
    info "$(printf '%-20s %-15s %-14s %-12s %s' NAME ADDRESS "LAST SEEN" "RX/TX MiB" "ALLOWED IPS")"
    while read -r name ip pub allowed; do
        n=$(( n + 1 ))
        seen=$(ago "${hs[$pub]:-0}")
        if is_disabled "$name"; then seen=disabled; fi
        info "$(printf '%-20s %-15s %-14s %-12s %s' "$name" "$ip" "$seen" \
            "$(( ${rx[$pub]:-0} / 1048576 ))/$(( ${tx[$pub]:-0} / 1048576 ))" "${allowed//,/, }")"
    done < <(db_entries)
    if [ "$n" -eq 0 ]; then info "none yet (add one: wg-client.sh add <name>)"; fi
    log "Routes"
    info "main default: $(default_route)  (recorded: $(state_get DEFAULT_ROUTE))"
    while IFS= read -r f; do
        $f rule show | grep -F "lookup $RT_TABLE" | sed "s/^/    $f rule: /" || info "$f rule: none (WARP off)"
        $f route show table "$RT_TABLE" 2>/dev/null | sed "s/^/    $f table $RT_TABLE: /" || true
    done < <(ip_families)
    log "Rules"
    while IFS='|' read -r x t c r; do
        # shellcheck disable=SC2086  # $r is a rule: split into iptables args
        if $x -t "$t" -C "$c" $r 2>/dev/null; then info "present  $x -t $t $c $r"; else info "missing  $x -t $t $c $r"; fi
    done < <(ipt_rules)
    info "ip_forward = $(sysctl -n net.ipv4.ip_forward)"
    if [ "$IPV6" -eq 1 ]; then
        info "IPv6: clients in $WG_SUBNET6, ipv6 forwarding = $(sysctl -n net.ipv6.conf.all.forwarding)"
    else
        info "IPv6: off${WG_SUBNET6:+ ($( [ "$WG_SUBNET6" = off ] && echo disabled || echo "kernel IPv6 disabled"))}"
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
# A WARP check: a FAIL only while WARP is on (or while testing WARP itself).
check_warp() {
    local desc="$1"
    shift
    if "$@"; then t_result PASS "$desc"
    elif [ "$WARP" = on ] || [ "$T_SCOPE" = warp ]; then t_result FAIL "$desc"
    else t_result WARN "$desc (WARP is off)"; fi
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
# ip rule / table checks for family $1 ("ip" or "ip -6"). The rule is there
# exactly once with WARP on, and absent with it off.
t_ip_rule() {
    local net="$WG_SUBNET" want=0
    if [ "$1" = "ip -6" ]; then net="$WG_SUBNET6"; fi
    if [ "$WARP" = on ]; then want=1; fi
    [ "$($1 rule show | grep -cF "from $net lookup $RT_TABLE")" -eq "$want" ]
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
t_enabled() { systemctl is-enabled -q "wg-quick@$WG_IF"; }
t_watchdog() { systemctl is-enabled -q "$WATCHDOG.timer" && systemctl is-active -q "$WATCHDOG.timer"; }
t_warp_enabled() { systemctl is-enabled -q "wg-quick@$WARP_IF"; }
t_ordering() {
    [[ " $(systemctl show -p After --value "wg-quick@$WG_IF") " == *" wg-quick@$WARP_IF.service "* ]] \
        && [[ " $(systemctl show -p Wants --value "wg-quick@$WG_IF") " == *" wg-quick@$WARP_IF.service "* ]]
}

# Run the checks; "warp" stops after the WARP checks. WARP checks only fail
# the run while WARP is on.
T_SCOPE=all
run_tests() {
    local rule f net
    T_SCOPE="$1"
    check_warp "$WARP_IF: curl --interface $WARP_IF shows warp=on" t_warp_on
    if [ "$IPV6" -eq 1 ]; then check_warp "$WARP_IF: IPv6 through $WARP_IF shows warp=on" t_warp6_on; fi
    check_warp "$WARP_IF: handshake within the last 3 minutes" t_warp_handshake
    check "server's own traffic is not tunneled (plain curl shows warp=off)" t_untunneled
    check "default route unchanged since install ($(state_get DEFAULT_ROUTE))" t_default_route
    [ "$1" != warp ] || return 0
    if [ ! -f "$WG_CONF" ]; then
        t_result SKIP "$WG_IF checks (the WireGuard server isn't set up yet)"
        return 0
    fi
    check "$WG_IF listening on udp/$WG_PORT" t_listen
    check "net.ipv4.ip_forward = 1" t_forward
    if [ "$IPV6" -eq 1 ]; then check "net.ipv6.conf.all.forwarding = 1" t_forward6; fi
    while IFS= read -r f; do
        net="$WG_SUBNET"
        if [ "$f" = "ip -6" ]; then net="$WG_SUBNET6"; fi
        if [ "$WARP" = on ]; then
            check "$f rule: from $net lookup $RT_TABLE (exactly once; WARP on)" t_ip_rule "$f"
        else
            check "$f rule: none from $net (WARP off)" t_ip_rule "$f"
        fi
        check_warp "$f table $RT_TABLE: default via $WARP_IF, blackhole fallback, $net via $WG_IF" t_table "$f"
    done < <(ip_families)
    while IFS= read -r rule; do
        check "exactly once: ${rule//|/ }" t_ipt_once "$rule"
    done < <(ipt_rules)
    check "wg-quick@$WG_IF enabled at boot" t_enabled
    check_warp "wg-quick@$WARP_IF enabled at boot" t_warp_enabled
    check "wg-quick@$WG_IF ordered after (and wants) wg-quick@$WARP_IF" t_ordering
    check "WARP watchdog timer enabled and running" t_watchdog
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
    require_setup
    log "Tests (WARP $WARP)"
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
    # CLIENT_DIR is inside WG_DIR.
    for p in "$WG_DIR" "$SYSCTL_FILE" "$DROPIN_DIR"; do
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
# and an IP endpoint is re-detected. Then setup runs, with the saved WARP setting.
cmd_restore() {
    local archive="${1:-}" members ep k
    [ -n "$archive" ] || die "usage: $PROG restore <archive.tar.gz>"
    [ -f "$archive" ] || die "no such file: $archive"
    archive=$(readlink -f "$archive")
    members=$(tar -tzf "$archive" 2>/dev/null) || die "$archive is not a readable .tar.gz"
    grep -qx "$STATE" <<<"$members" || die "$archive is not a wireguard-tools backup (no $STATE inside)"
    # Restoring over a live setup would orphan its rules and lose its rollback record.
    if [ -n "$SAVED_WARP" ]; then
        die "this server is already set up; run '$PROG rollback' (or 'rollback --purge') first"
    fi

    take_lock
    local what="keys, clients and settings" has_warp=0
    if grep -q "^$WGCF_DIR/wgcf-account.toml\$" <<<"$members"; then has_warp=1; what="keys, clients, settings and WARP account"; fi
    step "Restore $what from $archive" \
        "$PROG rollback --purge"
    confirm "Restore from this backup?"
    local paths=("$WG_DIR") keep_map=""
    # This server's own backup record (left by an earlier rollback) lists its
    # original files; keep it over the archive's, which describes the old server.
    if [ -f "$BACKUP_MAP" ]; then keep_map="$TMPD/backups"; cp -a "$BACKUP_MAP" "$keep_map"; fi
    run tar -xzpf "$archive" -P "${paths[@]}"
    if [ "$DRY_RUN" -eq 1 ]; then
        info "[dry-run] would then run: $PROG setup"
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
    done < <(grep "^$WG_DIR/" <<<"$members")
    chmod 600 "$BACKUP_MAP" 2>/dev/null || true
    info "restored; clients: $(db_entries | awk '{ printf "%s ", $1 }')"
    if [ "$has_warp" -eq 1 ]; then
        warn "the WARP account is now on this server too; don't keep running it on the old one"
    fi
    flock -u 9
    local flags=()
    if [ "$YES" -eq 1 ]; then flags+=(--yes); fi
    log "Setting up"
    info "if setup stops, fix the cause and run '$PROG setup' (the restored files stay in place)"
    # exec skips the EXIT trap: remove our temp dir and hand back the caller's
    # TMPDIR, or the new process's mktemp would use the deleted dir.
    rm -rf "$TMPD"
    if [ -n "$CALLER_TMPDIR" ]; then export TMPDIR="$CALLER_TMPDIR"; else unset TMPDIR; fi
    exec "$SELF" "${flags[@]+"${flags[@]}"}" setup
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
    step "Roll back: stop $WG_IF and $WARP_IF, remove routing/NAT/firewall rules, restore original files" \
        "$PROG setup"
    if [ "$PURGE" -eq 1 ]; then
        warn "--purge also deletes the WARP account, server and client keys, and client configs (irreversible)"
    fi
    confirm "Proceed with rollback?"

    run systemctl disable --now "$WATCHDOG.timer" 2>/dev/null || true
    run systemctl disable --now "wg-quick@$WG_IF" 2>/dev/null || true
    run systemctl disable --now "wg-quick@$WARP_IF" 2>/dev/null || true
    if if_exists "$WG_IF"; then run ip link del dev "$WG_IF"; fi
    if if_exists "$WARP_IF"; then run ip link del dev "$WARP_IF"; fi
    run_hooks down

    if [ "$(state_get UFW)" = 1 ] && command -v ufw >/dev/null; then
        run ufw delete allow "$WG_PORT/udp" || true
        while IFS= read -r out; do
            run ufw route delete allow in on "$WG_IF" out on "$out" || true
        done < <(exit_ifs)
        state_unset UFW
    fi

    # Our own drop-in's .bak copies would keep its directory from being removed.
    # shellcheck disable=SC2086  # glob on purpose
    if [ "$(backup_orig "$DROPIN")" = NEW ]; then run rm -f $DROPIN.*.bak; fi
    restore_original "$DROPIN" 1
    if [ -d "$DROPIN_DIR" ]; then run rmdir --ignore-fail-on-non-empty "$DROPIN_DIR"; fi
    for f in "$WATCHDOG_TIMER" "$WATCHDOG_SERVICE" "$WATCHDOG_BIN"; do restore_original "$f" 1; done
    run rm -f /run/$WATCHDOG
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
        # Not set up any more (wg-client.sh and restore check this).
        state_unset WARP
        state_unset EXIT
        local kept="keys, clients ($CLIENT_DB, $CLIENT_DIR)"
        if [ -d "$WGCF_DIR" ]; then kept="$kept, WARP account ($WGCF_DIR)"; fi
        info "kept: $kept (rollback --purge removes them)"
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
        setup)    cmd_setup ;;
        warp)     cmd_warp "$@" ;;
        direct|server) die "'$cmd' is now '$PROG setup', then '$PROG warp off' for no WARP" ;;
        test)     cmd_test ;;
        status)   cmd_status ;;
        backup)   cmd_backup "$@" ;;
        restore)  cmd_restore "$@" ;;
        rollback) cmd_rollback ;;
        *) die "unknown command: $cmd (see: $PROG help)" ;;
    esac
}

main "$@"
