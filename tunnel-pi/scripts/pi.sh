#!/bin/sh
# pi.sh <step> [args]: the Raspberry Pi's side of tunnel-pi. Runs as root.
#
# scripts/tpi-ctl.sh on your computer streams this file to the Pi over ssh, with the
# settings it needs set first. To run it on the Pi yourself instead:
#   sudo TP_CONF=/path/to/tunnel-pi.conf sh pi.sh <step>
#
# Steps (each is safe to run again):
#   base                 packages, automatic updates, watchdog; cloudflared too when
#                        CF_TOKEN is set (tpi-ctl.sh sends it when SSH_CLOUDFLARE is set)
#   link                 WireGuard and the wgpi link to the VPS. Prints PUBKEY=<the Pi's
#                        public key>; with VPS_PUBKEY set it also writes and starts wgpi
#   jump isolated|lan    TPI_ROUTING=wgt: the jump server (wgjump, or wgjumplan for lan). Prints
#                        JUMPFILE=<path> when it made a new jump file, else JUMP=exists
#   jump-done isolated|lan   delete the jump file once the VPS has it
#   dns                  TPI_ROUTING=tpi: the DNS resolver for Pi-exit clients
#   lan-mode <interface> isolated|lan   switch an exit interface's LAN mode (2.3)
#   wifi-list            the saved Wi-Fi networks, and which one is in use
#   wifi-add             save the Wi-Fi network WIFI_SSID (password WIFI_PSK; empty: open)
#   wifi-remove          remove the saved Wi-Fi network WIFI_SSID (all: every one)
#   check                PASS/FAIL for everything set up so far
#   undo [all]           remove the WireGuard parts (all: cloudflared too)
#
# Logs go to stderr; only results meant for tpi-ctl.sh (PUBKEY=, JUMPFILE=) go to stdout.

main() {
    set -eu
    umask 077
    [ "$(id -u)" = 0 ] || die "run as root"
    if [ -n "${TP_CONF:-}" ]; then . "$TP_CONF"; fi
    : "${TPI_ROUTING:=tpi}" "${VPS_PUBKEY:=}" "${CF_TOKEN:=}" "${SSH_CLOUDFLARE:=}" "${WIFI_SSID:=}" "${WIFI_PSK:=}"
    export DEBIAN_FRONTEND=noninteractive
    step="${1:-}"; [ $# -gt 0 ] && shift
    case "$step" in
        base) do_base ;;
        link) do_link ;;
        jump) do_jump "${1:-}" ;;
        jump-done) jump_vars "${1:-}"; rm -f "/etc/wireguard/$JFILE"; log "deleted $JFILE" ;;
        dns) do_dns ;;
        lan-mode) do_lan_mode "${1:-}" "${2:-}" ;;
        wifi-list) do_wifi_list ;;
        wifi-add) do_wifi_add ;;
        wifi-remove) do_wifi_remove ;;
        check) do_check ;;
        undo) do_undo "${1:-}" ;;
        *) die "unknown step '$step' (see the top of pi.sh)" ;;
    esac
}

WG=/etc/wireguard
EXIT_SCRIPT=/usr/local/sbin/tunnel-pi-exit
DISPATCHER=/etc/NetworkManager/dispatcher.d/90-tunnel-pi
NM_CONNS=/etc/NetworkManager/system-connections

log() { printf '==> %s\n' "$*" >&2; }
die() { printf 'pi.sh: %s\n' "$*" >&2; exit 1; }
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

do_base() {
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
    log "base done. Reboot once so the watchdog starts: sudo reboot"
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
    printf 'TUNNEL_TOKEN=%s\n' "$CF_TOKEN" | put 600 /etc/cloudflared/tunnel-pi.env
    put 644 /etc/systemd/system/tunnel-pi-cloudflared.service <<EOF
[Unit]
Description=tunnel-pi: Cloudflare Tunnel for admin SSH
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=/etc/cloudflared/tunnel-pi.env
ExecStart=$(command -v cloudflared) --no-autoupdate tunnel run
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    start_or_restart tunnel-pi-cloudflared.service
}

do_link() {
    need VPS_HOST PI_PORT
    apt-get -y -q install wireguard-tools iptables >/dev/null
    printf 'net.ipv4.ip_forward=1\n' | put 644 /etc/sysctl.d/99-tunnel-pi.conf
    sysctl -q --system
    # At power-on DHCP or DNS may not be ready: keep retrying instead of staying down.
    put 644 /etc/systemd/system/wg-quick@.service.d/tunnel-pi-retry.conf <<'EOF'
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
# tunnel-pi: when a network comes or goes, move the exit rules to the current uplink.
case "$2" in up|down|dhcp4-change) /usr/local/sbin/tunnel-pi-exit refresh 2>&1 | logger -t tunnel-pi-exit ;; esac
EOF
    else
        log "no NetworkManager: after the Pi changes networks, reboot it so the exit follows"
    fi
    echo "PUBKEY=$(keypair wgpi)"
    if [ -z "$VPS_PUBKEY" ]; then
        log "keys ready; wgpi is written once VPS_PUBKEY is known (tpi-ctl.sh link does this)"
        return 0
    fi

    exit_lines=""
    if [ "$TPI_ROUTING" = tpi ]; then
        [ -x "$EXIT_SCRIPT" ] || die "$EXIT_SCRIPT is missing (tpi-ctl.sh link installs it)"
        mode=$(current_mode wgpi)
        exit_lines="PostUp = $EXIT_SCRIPT up %i $mode
PostDown = $EXIT_SCRIPT down %i"
    fi
    CHANGED=0
    put 600 "$WG/wgpi.conf" <<EOF
# tunnel-pi: the link to the VPS (written by pi.sh link)
[Interface]
Address = 10.99.0.2/30
MTU = 1420
PostUp = wg set %i private-key $WG/wgpi.key
$exit_lines

[Peer]
# the VPS
PublicKey = $VPS_PUBKEY
Endpoint = $VPS_HOST:$PI_PORT
AllowedIPs = 10.99.0.1/32
PersistentKeepalive = 25
EOF
    start_or_restart wg-quick@wgpi
    log "wgpi up; the first handshake can take a few seconds"
}

# jump_vars isolated|lan: the names and addresses of that jump server.
jump_vars() {
    case "$1" in
        isolated) JIF=wgjump    JADDR=10.98.0.1/30 JPEER=10.98.0.2 JPORT=51822 JFILE=jump-for-vps.conf ;;
        lan)      JIF=wgjumplan JADDR=10.98.0.5/30 JPEER=10.98.0.6 JPORT=51823 JFILE=jump-for-vps-lan.conf ;;
        *) die "jump needs isolated or lan" ;;
    esac
}

do_jump() {
    jump_vars "$1"
    [ -x "$EXIT_SCRIPT" ] || die "$EXIT_SCRIPT is missing (tpi-ctl.sh link installs it)"
    if [ -f "$WG/$JIF.conf" ] && [ "${TP_RENEW:-}" != 1 ]; then
        systemctl enable -q --now "wg-quick@$JIF"
        log "$JIF already exists; its jump file was handed over when it was made (TP_RENEW=1 makes new keys)"
        echo "JUMP=exists"
        return 0
    fi
    # The VPS's private key for this jump is made here and leaves only in the jump
    # file, which tpi-ctl.sh pipes straight to `wgt jump add` and then deletes.
    rm -f "$WG/$JIF.key"
    server_pub=$(keypair "$JIF")
    vps_key=$(wg genkey)
    vps_pub=$(printf '%s\n' "$vps_key" | wg pubkey)
    CHANGED=0
    put 600 "$WG/$JIF.conf" <<EOF
# tunnel-pi: the jump server the VPS connects to over wgpi (written by pi.sh jump $1)
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
# tunnel-pi ($JIF), reached over wgpi
PublicKey = $server_pub
Endpoint = 10.99.0.2:$JPORT
EOF
    vps_key=""
    start_or_restart "wg-quick@$JIF"
    echo "JUMPFILE=$WG/$JFILE"
}

do_dns() {
    apt-get -y -q install unbound >/dev/null
    CHANGED=0
    put 644 /etc/unbound/unbound.conf.d/tunnel-pi.conf <<'EOF'
# tunnel-pi: answers DNS for Pi-exit clients (the VPS redirects their port 53 here)
server:
    interface: 127.0.0.1
    interface: 10.99.0.2
    ip-freebind: yes
    access-control: 127.0.0.0/8 allow
    access-control: 10.99.0.0/30 allow
EOF
    unbound-checkconf >/dev/null
    start_or_restart unbound
}

do_lan_mode() {
    ifc="$1" mode="$2"
    case "$mode" in isolated|lan) ;; *) die "lan-mode <interface> isolated|lan" ;; esac
    grep -q "^PostUp = $EXIT_SCRIPT up %i " "$WG/$ifc.conf" 2>/dev/null \
        || die "$ifc has no exit (it's wgpi with TPI_ROUTING=tpi, or wgjump / wgjumplan)"
    sed -i "s|^PostUp = $EXIT_SCRIPT up %i .*|PostUp = $EXIT_SCRIPT up %i $mode|" "$WG/$ifc.conf"
    systemctl restart "wg-quick@$ifc"
    journalctl -q -u "wg-quick@$ifc" -n 20 --no-pager | grep 'tunnel-pi-exit:' | tail -n 1 >&2 || true
}

need_nm() { command -v nmcli >/dev/null || die "Wi-Fi needs NetworkManager (Raspberry Pi OS Bookworm or later)"; }
uplink() { "$EXIT_SCRIPT" uplink 2>/dev/null || echo none; }

# Saved Wi-Fi connections, one per line: <name>:<device it's on, or empty>
wifi_conns() { nmcli -t -f NAME,TYPE,DEVICE connection show | awk -F: '$2 == "802-11-wireless" {print $1 ":" $3}'; }

do_wifi_list() {
    need_nm
    up=$(uplink)
    printf 'uplink now: %s\n' "$up"
    list=$(wifi_conns)
    if [ -z "$list" ]; then echo "saved Wi-Fi: none (the Pi uses ethernet only)"; return 0; fi
    echo "saved Wi-Fi (the Pi joins any of these in range; ethernet wins when both work):"
    printf '%s\n' "$list" | while IFS=: read -r n d; do
        if [ -z "$d" ]; then echo "  $n"; elif [ "$d" = "$up" ]; then echo "  $n  (connected: the uplink)"; else echo "  $n  (connected)"; fi
    done
}

do_wifi_add() {
    need_nm; need WIFI_SSID
    case "$WIFI_SSID" in *"
"*) die "a network name can't hold a line break" ;; esac
    name=$(printf '%s' "$WIFI_SSID" | tr -c 'A-Za-z0-9_-' _)
    # A keyfile NetworkManager reads (root-only, mode 600): the password never goes on
    # a command line. route-metric 600 keeps ethernet (100) first when both work.
    {
        printf '[connection]\nid=%s\ntype=wifi\nautoconnect=true\n\n[wifi]\nmode=infrastructure\nssid=%s\n\n' "$WIFI_SSID" "$WIFI_SSID"
        [ -z "$WIFI_PSK" ] || printf '[wifi-security]\nkey-mgmt=wpa-psk\npsk=%s\n\n' "$WIFI_PSK"
        printf '[ipv4]\nmethod=auto\nroute-metric=600\n\n[ipv6]\nmethod=auto\n'
    } | put 600 "$NM_CONNS/tunnel-pi-$name.nmconnection"
    WIFI_PSK=""
    nmcli connection reload
    log "saved Wi-Fi '$WIFI_SSID': the Pi joins it when it's in range (ethernet still wins when both work)"
}

do_wifi_remove() {
    need_nm; need WIFI_SSID
    up=$(uplink) list=$(wifi_conns) found=""
    # First make sure none of them is the connection the Pi is using right now.
    while IFS=: read -r n d; do
        [ -n "$n" ] || continue
        [ "$WIFI_SSID" = all ] || [ "$n" = "$WIFI_SSID" ] || continue
        found=1
        [ -z "$d" ] || [ "$d" != "$up" ] || die "'$n' is the Pi's uplink right now: plug in ethernet first, then run this again"
    done <<EOF
$list
EOF
    [ -n "$found" ] || { log "no saved Wi-Fi named '$WIFI_SSID' (tpi-ctl.sh wifi lists them)"; return 0; }
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

do_check() {
    if systemctl cat tunnel-pi-cloudflared.service >/dev/null 2>&1; then
        check "cloudflared (the emergency door) is running" "systemctl is-active -q tunnel-pi-cloudflared.service"
    elif [ -n "$SSH_CLOUDFLARE" ]; then
        fail "cloudflared isn't set up, but SSH_CLOUDFLARE is (tpi-ctl.sh base)"
    else
        printf 'INFO  no cloudflared (SSH_CLOUDFLARE is empty): no emergency door if the link goes down\n'
    fi
    if [ -f "$WG/wgpi.conf" ]; then
        check "IP forwarding is on" "[ \"\$(sysctl -n net.ipv4.ip_forward)\" = 1 ]"
        check "wgpi is up" "ip link show wgpi"
        age=$(handshake_age wgpi)
        if [ "$age" -lt 180 ]; then pass "wgpi handshake ${age}s ago"; else fail "wgpi has no recent handshake (is PI_PORT/udp open on the VPS?)"; fi
        check "the VPS answers on the link (ping 10.99.0.1)" "ping -c 2 -W 2 10.99.0.1"
    else
        fail "wgpi isn't set up (tpi-ctl.sh link)"
    fi
    for ifc in wgjump wgjumplan; do
        [ -f "$WG/$ifc.conf" ] || continue
        check "$ifc is up ($(current_mode "$ifc"))" "ip link show $ifc"
        age=$(handshake_age "$ifc")
        if [ "$age" -lt 180 ]; then pass "$ifc handshake ${age}s ago"
        else printf 'INFO  %s: no recent handshake (normal while no device uses the jump: wgt brings it up on demand)\n' "$ifc"; fi
    done
    if [ "$TPI_ROUTING" = tpi ] && [ -f "$WG/wgpi.conf" ]; then
        check "wgpi runs the exit rules ($(current_mode wgpi))" "grep -q '^PostUp = $EXIT_SCRIPT up' $WG/wgpi.conf"
        check "unbound answers on 10.99.0.2" "unbound-host -C /etc/unbound/unbound.conf -t A example.com || ss -Hlun 'sport = :53' | grep -q 10.99.0.2"
    fi
    if command -v nmcli >/dev/null; then
        wifi=$(wifi_conns | cut -d: -f1 | paste -sd, - | sed 's/,/, /g')
        printf 'INFO  uplink: %s. Saved Wi-Fi: %s\n' "$(uplink)" "${wifi:-none (ethernet only)}"
    fi
    ip=$(curl -s -m 10 https://ifconfig.me || true)
    printf 'INFO  this network'"'"'s public IP: %s\n' "${ip:-unknown}"
    return "$FAILED"
}

do_undo() {
    for ifc in wgjumplan wgjump wgpi; do
        systemctl disable -q --now "wg-quick@$ifc" 2>/dev/null || true
    done
    rm -f "$WG"/wgpi.* "$WG"/wgjump* "$WG"/jump-for-vps*.conf "$EXIT_SCRIPT" \
          /etc/unbound/unbound.conf.d/tunnel-pi.conf /etc/sysctl.d/99-tunnel-pi.conf \
          /etc/systemd/system/wg-quick@.service.d/tunnel-pi-retry.conf "$DISPATCHER"
    systemctl restart unbound 2>/dev/null || true
    if [ "${1:-}" = all ]; then
        systemctl disable -q --now tunnel-pi-cloudflared.service 2>/dev/null || true
        rm -f /etc/systemd/system/tunnel-pi-cloudflared.service /etc/cloudflared/tunnel-pi.env
        log "cloudflared stopped: delete the tunnel and the Access application in the dashboard too"
    fi
    systemctl daemon-reload
    log "undone"
}

# Everything above is definitions; this line runs it, with stdin closed so that
# nothing inside eats the rest of a script streamed over ssh.
main "$@" < /dev/null
