#!/bin/sh
# puck.sh <step> [args]: the puck's side of tunnel-puck. Runs as root.
#
# scripts/tpk-ctl.sh on your computer streams this file to the puck over ssh, after the
# settings it needs and ids.sh. To run it on the puck yourself instead:
#   cat ids.sh puck.sh | sudo TPK_CONF=/path/to/configs/<name>.conf sh -s -- <step>
#
# Steps (each is safe to run again):
#   init                 packages, automatic updates, watchdog; cloudflared too when
#                        CF_TOKEN is set (tpk-ctl.sh sends it when SSH_CLOUDFLARE is set)
#   link                 WireGuard and the link to one VPS, wgpk<n> (n from TPK_LINK: ids.sh).
#                        Prints PUBKEY=<the puck's public key>; with VPS_PUBKEY set it also
#                        writes and starts wgpk<n>
#   jump isolated|lan    TPK_ROUTING=wgt: that link's reverse tunnel (wgrtun<n>, or wgrtunlan<n>
#                        for lan). Prints JUMPFILE=<path> when it made a new jump file, else JUMP=exists
#   jump-done isolated|lan   delete the jump file once the VPS has it
#   dns                  TPK_ROUTING=tpk: the DNS resolver for that link's puck-exit clients
#   lan-mode <interface> isolated|lan   switch an exit interface's LAN mode (2.3)
#   wifi-list            the saved Wi-Fi networks, and which one is in use
#   wifi-add             save the Wi-Fi network WIFI_SSID (password WIFI_PSK; empty: open)
#   wifi-remove          remove the saved Wi-Fi network WIFI_SSID (all: every one)
#   wifi-sync            make tunnel-puck's saved Wi-Fi match WIFI_LIST ("ssid::password"
#                        lines, most preferred first): the first gets the highest priority
#   status               the puck at a glance, as lines for tpk-ctl.sh status: UPLINK=, WIFI=,
#                        IP=, then "HS <interface> <seconds since its handshake, none, or down>"
#   test                 PASS/FAIL for the puck itself (cloudflared, uplink, Wi-Fi, public IP)
#   test-link            PASS/FAIL for one link (TPK_LINK) and its rtuns
#   undo-link            remove one link (TPK_LINK) and its rtuns
#   undo [all]           remove every link and the shared parts (all: cloudflared too)
#
# Logs go to stderr; stdout is a step's result: lines tpk-ctl.sh reads (PUBKEY=,
# JUMPFILE=, status's), or shows (wifi-list, the tests' PASS/FAIL).

main() {
    set -eu
    umask 077
    [ "$(id -u)" = 0 ] || die "run as root"
    if [ -n "${TPK_CONF:-}" ]; then . "$TPK_CONF"; fi
    : "${PUCK_NAME:=$(basename "${TPK_CONF:-puck}" .conf)}"
    link_ids
    : "${TPK_ROUTING:=tpk}" "${VPS_PUBKEY:=}" "${CF_TOKEN:=}" "${SSH_CLOUDFLARE:=}" "${WIFI_SSID:=}" "${WIFI_PSK:=}" "${WIFI_LIST:=}"
    export DEBIAN_FRONTEND=noninteractive
    step="${1:-}"; [ $# -gt 0 ] && shift
    case "$step" in
        init) do_init ;;
        link) do_link ;;
        jump) do_jump "${1:-}" ;;
        jump-done) jump_vars "${1:-}"; rm -f "/etc/wireguard/$JFILE"; log "deleted $JFILE" ;;
        dns) do_dns ;;
        lan-mode) do_lan_mode "${1:-}" "${2:-}" ;;
        wifi-list) do_wifi_list ;;
        wifi-add) do_wifi_add ;;
        wifi-sync) do_wifi_sync ;;
        wifi-remove) do_wifi_remove ;;
        status) do_status ;;
        test) do_test ;;
        test-link) do_test_link ;;
        undo-link) do_undo_link ;;
        undo) do_undo "${1:-}" ;;
        *) die "unknown step '$step' (see the top of puck.sh)" ;;
    esac
}

WG=/etc/wireguard
EXIT_SCRIPT=/usr/local/sbin/tunnel-puck-exit
DISPATCHER=/etc/NetworkManager/dispatcher.d/90-tunnel-puck
NM_CONNS=/etc/NetworkManager/system-connections

log() { printf '==> %s\n' "$*" >&2; }
die() { printf 'puck.sh: %s\n' "$*" >&2; exit 1; }
need() { for v in "$@"; do eval "x=\${$v:-}"; [ -n "$x" ] || die "$v is not set"; done; }

# put <mode> <path>: write stdin to <path> if it differs; CHANGED=1 when it did.
CHANGED=0
put() {
    tmp=$(mktemp); cat > "$tmp"
    if [ -f "$2" ] && cmp -s "$tmp" "$2"; then rm -f "$tmp"; chmod "$1" "$2"; return 0; fi
    mkdir -p "$(dirname "$2")"
    install -m "$1" "$tmp" "$2"; rm -f "$tmp"; CHANGED=1; log "wrote $2"
}

# keypair <name>: make $WG/<name>.key if missing (never printed), write .pub, print the public key.
keypair() {
    [ -s "$WG/$1.key" ] || { wg genkey > "$WG/$1.key"; log "made $WG/$1.key"; }
    chmod 600 "$WG/$1.key"
    wg pubkey < "$WG/$1.key" > "$WG/$1.pub"
    cat "$WG/$1.pub"
}

# start_or_restart <unit>: enable it; restart if a file changed, else start.
start_or_restart() {
    systemctl enable -q "$1"
    if [ "$CHANGED" = 1 ]; then systemctl restart "$1"; else systemctl start "$1"; fi
}

# The exit mode written in an interface's .conf (isolated if none yet).
current_mode() {
    sed -n "s|^PostUp = $EXIT_SCRIPT up %i \([a-z]*\)$|\1|p" "$WG/$1.conf" 2>/dev/null | head -n 1 | grep . || echo isolated
}

do_init() {
    log "updating packages (this can take a while)"
    apt-get update -q
    apt-get -y -q full-upgrade
    apt-get -y -q install unattended-upgrades curl
    echo 'unattended-upgrades unattended-upgrades/enable_auto_updates boolean true' | debconf-set-selections
    dpkg-reconfigure -f noninteractive unattended-upgrades

    if ! grep -q '^RuntimeWatchdogSec=15$' /etc/systemd/system.conf; then
        sed -i 's/^#\{0,1\}RuntimeWatchdogSec=.*/RuntimeWatchdogSec=15/' /etc/systemd/system.conf
        grep -q '^RuntimeWatchdogSec=15$' /etc/systemd/system.conf || echo 'RuntimeWatchdogSec=15' >> /etc/systemd/system.conf
        log "hardware watchdog on from the next reboot"
    fi

    if [ -n "$CF_TOKEN" ]; then do_cloudflared
    else log "no CF_TOKEN (SSH_CLOUDFLARE is empty): no cloudflared, so no emergency door"; fi
    log "init done. Reboot once so the watchdog starts: tpk-ctl.sh reboot"
}

# cloudflared, the emergency door: SSH through Cloudflare that doesn't depend on the link.
do_cloudflared() {
    if ! command -v cloudflared >/dev/null; then
        log "installing cloudflared from Cloudflare's apt repository"
        mkdir -p --mode=0755 /usr/share/keyrings
        curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
        chmod 644 /usr/share/keyrings/cloudflare-main.gpg
        echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
            > /etc/apt/sources.list.d/cloudflared.list
        chmod 644 /etc/apt/sources.list.d/cloudflared.list
        apt-get update -q
        apt-get -y -q install cloudflared
    fi

    # The token lives in a root-only file the unit reads, never on a command line.
    printf 'TUNNEL_TOKEN=%s\n' "$CF_TOKEN" | put 600 /etc/cloudflared/tunnel-puck.env
    put 644 /etc/systemd/system/tunnel-puck-cloudflared.service <<EOF
[Unit]
Description=tunnel-puck: Cloudflare Tunnel for admin SSH
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=/etc/cloudflared/tunnel-puck.env
ExecStart=$(command -v cloudflared) --no-autoupdate tunnel run
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    start_or_restart tunnel-puck-cloudflared.service
}

do_link() {
    apt-get -y -q install wireguard-tools iptables >/dev/null
    printf 'net.ipv4.ip_forward=1\n' | put 644 /etc/sysctl.d/99-tunnel-puck.conf
    sysctl -q --system
    # At power-on DHCP or DNS may not be ready: keep retrying instead of staying down.
    put 644 /etc/systemd/system/wg-quick@.service.d/tunnel-puck-retry.conf <<'EOF'
[Unit]
StartLimitIntervalSec=0

[Service]
Restart=on-failure
RestartSec=30
EOF
    systemctl daemon-reload
    # When a network comes or goes (ethernet unplugged, Wi-Fi joined), move the exit
    # rules to the new uplink, so no reboot is needed.
    if [ -d "${DISPATCHER%/*}" ]; then
        put 755 "$DISPATCHER" <<'EOF'
#!/bin/sh
# tunnel-puck: when a network comes or goes, move the exit rules to the current uplink.
# tunnel-puck's own WireGuard interfaces coming up aren't a network change: wg-quick
# runs their exit itself.
case "$1" in wg*) exit 0 ;; esac
case "$2" in up|down|dhcp4-change) /usr/local/sbin/tunnel-puck-exit refresh 2>&1 | logger -t tunnel-puck-exit ;; esac
EOF
    else
        log "no NetworkManager: after the puck changes networks, reboot it so the exit follows"
    fi
    need_link
    echo "PUBKEY=$(keypair "$PK_IF")"
    if [ -z "$VPS_PUBKEY" ]; then
        log "keys ready; $PK_IF is written once VPS_PUBKEY is known (tpk-ctl.sh link does this)"
        return 0
    fi
    need VPS_HOST PUCK_PORT

    exit_lines=""
    if [ "$TPK_ROUTING" = tpk ]; then
        [ -x "$EXIT_SCRIPT" ] || die "$EXIT_SCRIPT is missing (tpk-ctl.sh link installs it)"
        mode=$(current_mode "$PK_IF")
        exit_lines="PostUp = $EXIT_SCRIPT up %i $mode
PostDown = $EXIT_SCRIPT down %i"
    fi
    CHANGED=0
    put 600 "$WG/$PK_IF.conf" <<EOF
# tunnel-puck: the link to the VPS $VPS_HOST (written by puck.sh link)
[Interface]
Address = $PUCK_LINK/30
MTU = 1420
PostUp = wg set %i private-key $WG/$PK_IF.key
$exit_lines

[Peer]
# the VPS
PublicKey = $VPS_PUBKEY
Endpoint = $VPS_HOST:$PUCK_PORT
AllowedIPs = $VPS_LINK/32
PersistentKeepalive = 25
EOF
    start_or_restart "wg-quick@$PK_IF"
    log "$PK_IF up; the first handshake can take a few seconds"
}

# jump_vars isolated|lan: the names and addresses of that reverse tunnel: a
# WireGuard server on the puck that the VPS reaches in through, over its link
# (wgt calls it a jump).
jump_vars() {
    need_link
    case "$1" in
        isolated) JIF=$RTUN_IF     JADDR=$JUMP_NET.1/30 JPEER=$JUMP_NET.2 JPORT=$RTUN_PORT     JFILE=jump-for-vps$LINK_N.conf ;;
        lan)      JIF=$RTUN_LAN_IF JADDR=$JUMP_NET.5/30 JPEER=$JUMP_NET.6 JPORT=$RTUN_LAN_PORT JFILE=jump-for-vps$LINK_N-lan.conf ;;
        *) die "jump needs isolated or lan" ;;
    esac
}

do_jump() {
    jump_vars "$1"
    [ -x "$EXIT_SCRIPT" ] || die "$EXIT_SCRIPT is missing (tpk-ctl.sh link installs it)"
    if [ -f "$WG/$JIF.conf" ] && [ "${TPK_RENEW:-}" != 1 ]; then
        systemctl enable -q --now "wg-quick@$JIF"
        log "$JIF already exists; its jump file was handed over when it was made (TPK_RENEW=1 makes new keys)"
        echo "JUMP=exists"
        return 0
    fi
    # The VPS's private key for this jump is made here and leaves only in the jump
    # file, which tpk-ctl.sh pipes straight to `wgt jump add` and then deletes.
    rm -f "$WG/$JIF.key"
    server_pub=$(keypair "$JIF")
    vps_key=$(wg genkey)
    vps_pub=$(printf '%s\n' "$vps_key" | wg pubkey)
    CHANGED=0
    put 600 "$WG/$JIF.conf" <<EOF
# tunnel-puck: the reverse tunnel, the VPS's way in over $PK_IF; a wgt jump (written by puck.sh jump $1)
[Interface]
Address = $JADDR
ListenPort = $JPORT
MTU = 1420
PostUp = wg set %i private-key $WG/$JIF.key
PostUp = $EXIT_SCRIPT up %i $1
PostDown = $EXIT_SCRIPT down %i

[Peer]
# the VPS, as a jump client
PublicKey = $vps_pub
AllowedIPs = $JPEER/32
EOF
    put 600 "$WG/$JFILE" <<EOF
[Interface]
PrivateKey = $vps_key
Address = $JPEER/32

[Peer]
# tunnel-puck ($JIF), reached over the link
PublicKey = $server_pub
Endpoint = $PUCK_LINK:$JPORT
EOF
    vps_key=""
    start_or_restart "wg-quick@$JIF"
    echo "JUMPFILE=$WG/$JFILE"
}

# unbound: tunnel-puck.conf for the puck itself, and tunnel-puck-<n>.conf for each
# tpk link's clients (their VPS redirects their port 53 to the puck's end of it).
UNBOUND=/etc/unbound/unbound.conf.d/tunnel-puck
do_dns() {
    need_link
    apt-get -y -q install unbound >/dev/null
    CHANGED=0
    put 644 "$UNBOUND.conf" <<EOF
# tunnel-puck: answers DNS for puck-exit clients (each link's: $UNBOUND-<n>.conf)
server:
    interface: 127.0.0.1
    ip-freebind: yes
    access-control: 127.0.0.0/8 allow
EOF
    put 644 "$UNBOUND-$LINK_N.conf" <<EOF
# tunnel-puck: DNS for the puck-exit clients of $PK_IF
server:
    interface: $PUCK_LINK
    access-control: $LINK_NET allow
EOF
    unbound-checkconf >/dev/null
    start_or_restart unbound
}

do_lan_mode() {
    ifc="$1" mode="$2"
    case "$mode" in isolated|lan) ;; *) die "lan-mode <interface> isolated|lan" ;; esac
    grep -q "^PostUp = $EXIT_SCRIPT up %i " "$WG/$ifc.conf" 2>/dev/null \
        || die "$ifc has no exit (it's wgpk<n> with TPK_ROUTING=tpk, or wgrtun<n> / wgrtunlan<n>)"
    sed -i "s|^PostUp = $EXIT_SCRIPT up %i .*|PostUp = $EXIT_SCRIPT up %i $mode|" "$WG/$ifc.conf"
    systemctl restart "wg-quick@$ifc"
    journalctl -q -u "wg-quick@$ifc" -n 20 --no-pager | grep 'tunnel-puck-exit:' | tail -n 1 >&2 || true
}

need_nm() { command -v nmcli >/dev/null || die "Wi-Fi needs NetworkManager (Debian Bookworm or later, e.g. Raspberry Pi OS)"; }
uplink() { "$EXIT_SCRIPT" uplink 2>/dev/null || echo none; }

# Saved Wi-Fi connections, one per line: <name>:<device it's on, or empty>
wifi_conns() { nmcli -t -f NAME,TYPE,DEVICE connection show | awk -F: '$2 == "802-11-wireless" {print $1 ":" $3}'; }
wifi_names() { wifi_conns | cut -d: -f1 | paste -sd, - | sed 's/,/, /g'; }    # on one line

do_wifi_list() {
    need_nm
    up=$(uplink)
    printf 'uplink now: %s\n' "$up"
    list=$(wifi_conns)
    if [ -z "$list" ]; then echo "saved Wi-Fi: none (the puck uses ethernet only)"; return 0; fi
    echo "saved Wi-Fi (the puck joins any of these in range; ethernet wins when both work):"
    printf '%s\n' "$list" | while IFS=: read -r n d; do
        if [ -z "$d" ]; then echo "  $n"; elif [ "$d" = "$up" ]; then echo "  $n  (connected: the uplink)"; else echo "  $n  (connected)"; fi
    done
}

# wifi_keyfile <ssid> <password or empty> <priority>: write tunnel-puck's keyfile for a
# network (root-only, mode 600: the password never goes on a command line) and
# print its path. route-metric 600 keeps ethernet (100) first when both work; among
# networks in range, NetworkManager joins the highest autoconnect-priority.
wifi_keyfile() {
    case "$1" in *"
"*|'') die "a network name can't be empty or hold a line break" ;; esac
    file="$NM_CONNS/tunnel-puck-$(printf '%s' "$1" | tr -c 'A-Za-z0-9_-' _).nmconnection"
    {
        printf '[connection]\nid=%s\ntype=wifi\nautoconnect=true\nautoconnect-priority=%s\n\n' "$1" "$3"
        printf '[wifi]\nmode=infrastructure\nssid=%s\n\n' "$1"
        [ -z "$2" ] || printf '[wifi-security]\nkey-mgmt=wpa-psk\npsk=%s\n\n' "$2"
        printf '[ipv4]\nmethod=auto\nroute-metric=600\n\n[ipv6]\nmethod=auto\n'
    } | put 600 "$file"
    echo "$file"
}

do_wifi_add() {
    need_nm; need WIFI_SSID
    wifi_keyfile "$WIFI_SSID" "$WIFI_PSK" 0 >/dev/null
    WIFI_PSK=""
    nmcli connection reload
    log "saved Wi-Fi '$WIFI_SSID': the puck joins it when it's in range (ethernet still wins when both work)"
}

# do_wifi_sync: tunnel-puck's saved networks become WIFI_LIST, in its order. Networks
# saved some other way (by the OS image's installer) are left alone; one this would remove
# is kept while it's the puck's uplink.
do_wifi_sync() {
    need_nm
    up=$(uplink) keep="" prio=100
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        keep="$keep
$(wifi_keyfile "${line%%::*}" "${line#*::}" "$prio")"
        log "Wi-Fi '${line%%::*}': priority $prio"
        prio=$((prio - 1))
    done <<EOF
$WIFI_LIST
EOF
    WIFI_LIST=""
    for f in "$NM_CONNS"/tunnel-puck-*.nmconnection; do
        [ -e "$f" ] || continue
        printf '%s\n' "$keep" | grep -qxF -- "$f" && continue
        id=$(sed -n 's/^id=//p' "$f" | head -n 1)
        dev=$(nmcli -t -f NAME,DEVICE connection show --active | awk -F: -v n="$id" '$1 == n {print $2}')
        if [ -n "$dev" ] && [ "$dev" = "$up" ]; then
            log "kept Wi-Fi '$id', not in the list, because it's the puck's uplink right now: plug in ethernet and sync again"
            continue
        fi
        rm -f "$f"; log "removed Wi-Fi '$id' (not in the list)"
    done
    nmcli connection reload
}

do_wifi_remove() {
    need_nm; need WIFI_SSID
    up=$(uplink) list=$(wifi_conns) found=""
    # First make sure none of them is the connection the puck is using right now.
    while IFS=: read -r n d; do
        [ -n "$n" ] || continue
        [ "$WIFI_SSID" = all ] || [ "$n" = "$WIFI_SSID" ] || continue
        found=1
        [ -z "$d" ] || [ "$d" != "$up" ] || die "'$n' is the puck's uplink right now: plug in ethernet first, then run this again"
    done <<EOF
$list
EOF
    [ -n "$found" ] || { log "no saved Wi-Fi named '$WIFI_SSID' (tpk-ctl.sh wifi lists them)"; return 0; }
    while IFS=: read -r n d; do
        [ -n "$n" ] || continue
        [ "$WIFI_SSID" = all ] || [ "$n" = "$WIFI_SSID" ] || continue
        nmcli connection delete id "$n" >/dev/null
        log "removed Wi-Fi '$n'"
    done <<EOF
$list
EOF
}

FAILED=0
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; FAILED=1; }
check() { if eval "$2" >/dev/null 2>&1; then pass "$1"; else fail "$1"; fi; }

handshake_age() {   # seconds since <interface>'s newest handshake (99999: none)
    t=$(wg show "$1" latest-handshakes 2>/dev/null | awk '$2 > m {m = $2} END {print m + 0}')
    if [ "${t:-0}" -gt 0 ]; then echo $(( $(date +%s) - t )); else echo 99999; fi
}

do_test() {
    if systemctl cat tunnel-puck-cloudflared.service >/dev/null 2>&1; then
        check "cloudflared (the emergency door) is running" "systemctl is-active -q tunnel-puck-cloudflared.service"
    elif [ -n "$SSH_CLOUDFLARE" ]; then
        fail "cloudflared isn't set up, but SSH_CLOUDFLARE is (tpk-ctl.sh init)"
    else
        printf 'INFO  no cloudflared (SSH_CLOUDFLARE is empty): no emergency door if the links go down\n'
    fi
    links=$(for f in "$WG"/wgpk*.conf; do [ -e "$f" ] && basename "$f" .conf; done | paste -sd' ' - || true)
    if [ -n "$links" ]; then
        check "IP forwarding is on" "[ \"\$(sysctl -n net.ipv4.ip_forward)\" = 1 ]"
        printf 'INFO  links: %s\n' "$links"
    else
        printf 'INFO  no links yet (tpk-ctl.sh <puck> vps add <vps>)\n'
    fi
    if command -v nmcli >/dev/null; then
        wifi=$(wifi_names)
        printf 'INFO  uplink: %s. Saved Wi-Fi: %s\n' "$(uplink)" "${wifi:-none (ethernet only)}"
    fi
    ip=$(curl -s -m 10 https://ifconfig.me || true)
    printf 'INFO  this network'"'"'s public IP: %s\n' "${ip:-unknown}"
    return "$FAILED"
}

# do_status: read-only, quick: no test traffic beyond one public-IP lookup.
do_status() {
    echo "UPLINK=$(uplink)"
    if command -v nmcli >/dev/null; then echo "WIFI=$(wifi_names)"; fi
    echo "IP=$(curl -4 -s -m 8 https://ifconfig.me || true)"
    for f in "$WG"/wgpk*.conf "$WG"/wgrtun*.conf; do
        [ -e "$f" ] || continue
        ifc=$(basename "$f" .conf)
        if ! ip link show "$ifc" >/dev/null 2>&1; then echo "HS $ifc down"
        else age=$(handshake_age "$ifc"); if [ "$age" = 99999 ]; then echo "HS $ifc none"; else echo "HS $ifc $age"; fi; fi
    done
}

# do_test_link: one link (TPK_LINK) and its rtuns.
do_test_link() {
    need_link
    if [ ! -f "$WG/$PK_IF.conf" ]; then fail "$PK_IF isn't set up (tpk-ctl.sh link)"; return "$FAILED"; fi
    check "$PK_IF is up" "ip link show $PK_IF"
    age=$(handshake_age "$PK_IF")
    if [ "$age" -lt 180 ]; then pass "$PK_IF handshake ${age}s ago"; else fail "$PK_IF has no recent handshake (is PUCK_PORT/udp open on the VPS?)"; fi
    check "the VPS answers on the link (ping $VPS_LINK)" "ping -c 2 -W 2 $VPS_LINK"
    for ifc in "$RTUN_IF" "$RTUN_LAN_IF"; do
        [ -f "$WG/$ifc.conf" ] || continue
        check "$ifc is up ($(current_mode "$ifc"))" "ip link show $ifc"
        age=$(handshake_age "$ifc")
        if [ "$age" -lt 180 ]; then pass "$ifc handshake ${age}s ago"
        else printf 'INFO  %s: no recent handshake (normal while no device uses the jump: wgt brings it up on demand)\n' "$ifc"; fi
    done
    if [ "$TPK_ROUTING" = tpk ]; then
        check "$PK_IF runs the exit rules ($(current_mode "$PK_IF"))" "grep -q '^PostUp = $EXIT_SCRIPT up' $WG/$PK_IF.conf"
        check "unbound answers on $PUCK_LINK" "unbound-host -C /etc/unbound/unbound.conf -t A example.com || ss -Hlun 'sport = :53' | grep -qF $PUCK_LINK"
    fi
    return "$FAILED"
}

# do_undo_link: one link (TPK_LINK), its rtuns and its DNS.
do_undo_link() {
    need_link
    for ifc in "$RTUN_LAN_IF" "$RTUN_IF" "$PK_IF"; do
        systemctl disable -q --now "wg-quick@$ifc" 2>/dev/null || true
        rm -f "$WG/$ifc.conf" "$WG/$ifc.key" "$WG/$ifc.pub"
    done
    for m in isolated lan; do jump_vars "$m"; rm -f "$WG/$JFILE"; done
    if [ -f "$UNBOUND-$LINK_N.conf" ]; then rm -f "$UNBOUND-$LINK_N.conf"; systemctl restart unbound 2>/dev/null || true; fi
    log "$PK_IF removed"
}

# do_undo [all]: every link, and what they share.
do_undo() {
    for f in "$WG"/wgrtun*.conf "$WG"/wgpk*.conf; do
        [ -e "$f" ] || continue
        systemctl disable -q --now "wg-quick@$(basename "$f" .conf)" 2>/dev/null || true
    done
    rm -f "$WG"/wgpk* "$WG"/wgrtun* "$WG"/jump-for-vps*.conf "$EXIT_SCRIPT" \
          "$UNBOUND".conf "$UNBOUND"-*.conf /etc/sysctl.d/99-tunnel-puck.conf \
          /etc/systemd/system/wg-quick@.service.d/tunnel-puck-retry.conf "$DISPATCHER"
    systemctl restart unbound 2>/dev/null || true
    if [ "${1:-}" = all ]; then
        systemctl disable -q --now tunnel-puck-cloudflared.service 2>/dev/null || true
        rm -f /etc/systemd/system/tunnel-puck-cloudflared.service /etc/cloudflared/tunnel-puck.env
        log "cloudflared stopped: delete the tunnel and the Access application in the dashboard too"
    fi
    systemctl daemon-reload
    log "undone"
}

# Everything above is definitions; this line runs it, with stdin closed so that
# nothing inside eats the rest of a script streamed over ssh.
main "$@" < /dev/null
