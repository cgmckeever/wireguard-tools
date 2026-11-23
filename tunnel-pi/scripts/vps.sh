#!/bin/sh
# vps.sh <step>: the VPS's side of tunnel-pi. Runs as root.
#
# scripts/tp on your computer streams this file to the VPS over ssh, with the
# settings it needs set first. To run it on the VPS yourself instead:
#   TP_CONF=/path/to/tunnel-pi.conf PI_PUBKEY=<the Pi's key> sh vps.sh <step>
#
# Steps (each is safe to run again):
#   link    the wgpi link to the Pi (PI_PUBKEY needed). Prints PUBKEY=<the VPS's
#           public key>. With EXIT=tpi it also writes the routing for CLIENT_IPS
#           and the kill switch
#   check   PASS/FAIL for the link and, by EXIT, the routing or the wgt jump(s)
#   undo    remove wgpi (and EXIT=tpi's kill switch). wgt jumps are removed
#           with wgt itself first (see the README's Undo)
#
# It never changes WG_IF (the clients' interface) or wgt's files. Logs go to stderr; only PUBKEY= goes
# to stdout.

main() {
    set -eu
    umask 077
    [ "$(id -u)" = 0 ] || die "run as root"
    if [ -n "${TP_CONF:-}" ]; then . "$TP_CONF"; fi
    : "${EXIT:=tpi}" "${PI_PUBKEY:=}" "${CLIENT_IPS:=}" "${CLIENT_IP6S:=}" "${WG_SUBNET:=}"
    : "${JUMP_CODE:=}" "${JUMP_LAN_CODE:=}" "${WG_IF:=wg0}"
    case "$WG_IF" in ''|*[!A-Za-z0-9_.-]*) die "WG_IF must be an interface name" ;; esac
    case "$EXIT" in tpi|wgt) ;; *) die "EXIT must be tpi or wgt" ;; esac
    case "${1:-}" in
        link) do_link ;;
        check) do_check ;;
        undo) do_undo ;;
        *) die "unknown step '${1:-}' (see the top of vps.sh)" ;;
    esac
}

WG=/etc/wireguard
KILL=/usr/local/sbin/tunnel-pi-killswitch
KILL_UNIT=/etc/systemd/system/tunnel-pi-killswitch.service

log() { printf '==> %s\n' "$*" >&2; }
die() { printf 'vps.sh: %s\n' "$*" >&2; exit 1; }
need() { for v in "$@"; do eval "x=\${$v:-}"; [ -n "$x" ] || die "$v is not set"; done; }

CHANGED=0
put() {
    tmp=$(mktemp); cat > "$tmp"
    if [ -f "$2" ] && cmp -s "$tmp" "$2"; then rm -f "$tmp"; chmod "$1" "$2"; return 0; fi
    mkdir -p "$(dirname "$2")"
    install -m "$1" "$tmp" "$2"; rm -f "$tmp"; CHANGED=1; log "wrote $2"
}

keypair() {
    [ -s "$WG/$1.key" ] || { wg genkey > "$WG/$1.key"; log "made $WG/$1.key"; }
    chmod 600 "$WG/$1.key"
    wg pubkey < "$WG/$1.key" > "$WG/$1.pub"
    cat "$WG/$1.pub"
}

v4() { case "$1" in ''|*[!0-9./]*) die "not an IPv4 address: '$1'" ;; esac; }
v6() { case "$1" in ''|*[!0-9a-fA-F:./]*) die "not an IPv6 address: '$1'" ;; esac; }

preflight() {
    command -v wg >/dev/null || die "wg isn't installed (apt install wireguard-tools)"
    ip link show $WG_IF >/dev/null 2>&1 || die "no interface $WG_IF here: set WG_IF to the WireGuard interface your clients connect to"
    # 10.99.0.0/30 must be free (or already wgpi's)
    other=$(ip -o -4 addr show | awk '$4 ~ /^10\.99\.0\.[0-3]\// && $2 != "wgpi" {print $2}')
    [ -z "$other" ] || die "10.99.0.0/30 is already used on $other"
    # PI_PORT must be free (or already wgpi's)
    if [ "$(wg show wgpi listen-port 2>/dev/null || true)" != "$PI_PORT" ] \
        && ss -Hlun "sport = :$PI_PORT" | grep -q .; then
        die "UDP port $PI_PORT is already in use (ss -ulpn)"
    fi
    if [ "$EXIT" = wgt ]; then
        command -v wgt >/dev/null || die "EXIT=wgt needs wgt on this VPS (or use EXIT=tpi)"
    fi
}

do_link() {
    need PI_PUBKEY PI_PORT
    case "$PI_PORT" in *[!0-9]*) die "PI_PORT must be a number" ;; esac
    preflight
    echo "PUBKEY=$(keypair wgpi)"

    CHANGED=0
    if [ "$EXIT" = wgt ]; then
        put 600 "$WG/wgpi.conf" <<EOF
# tunnel-pi: the link the Pi dials (written by vps.sh link; not wgt's)
[Interface]
Address = 10.99.0.1/30
ListenPort = $PI_PORT
MTU = 1420
PostUp = wg set %i private-key $WG/wgpi.key

[Peer]
# the Pi
PublicKey = $PI_PUBKEY
AllowedIPs = 10.99.0.2/32
EOF
    else
        conf=$(hand_conf)    # not piped: a die inside must stop the script, not write half a file
        printf '%s\n' "$conf" | put 600 "$WG/wgpi.conf"
        killswitch
    fi
    open_port
    systemctl enable -q wg-quick@wgpi
    if [ "$CHANGED" = 1 ]; then systemctl restart wg-quick@wgpi; else systemctl start wg-quick@wgpi; fi
    log "wgpi up; it waits for the Pi to dial in"
}

# The EXIT=tpi wgpi.conf: route CLIENT_IPS through the Pi, NAT onto the link, DNS to the Pi.
hand_conf() {
    need CLIENT_IPS
    if [ -z "$WG_SUBNET" ]; then
        # Every IPv4 subnet WG_IF is on (its kernel routes), so traffic between
        # clients stays on WG_IF
        WG_SUBNET=$(ip -4 route show dev "$WG_IF" proto kernel scope link 2>/dev/null | awk '{printf "%s ", $1}')
        [ -n "$WG_SUBNET" ] || die "WG_SUBNET is not set and $WG_IF has no IPv4 subnet to read"
    fi
    for net in $WG_SUBNET; do v4 "$net"; done
    cat <<EOF
# tunnel-pi: the link the Pi dials, and the clients routed through it (written by vps.sh link)
[Interface]
Address = 10.99.0.1/30
ListenPort = $PI_PORT
MTU = 1420
# Table = off: no routes from AllowedIPs; the rules below choose who uses the link
Table = off
PostUp = wg set %i private-key $WG/wgpi.key
# Table 260: the Pi for everything, except the VPS's own clients, which stay on $WG_IF
$(for net in $WG_SUBNET; do echo "PostUp = ip route replace $net dev $WG_IF table 260"; done)
PostUp = ip route replace default dev %i table 260
PostDown = ip route flush table 260
# Forward into the link and NAT onto it (the Pi sees only 10.99.0.1)
PostUp = iptables -I FORWARD -i $WG_IF -o %i -j ACCEPT
PostUp = iptables -I FORWARD -i %i -o $WG_IF -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
PostUp = iptables -t nat -A POSTROUTING -o %i -j MASQUERADE
PostDown = iptables -D FORWARD -i $WG_IF -o %i -j ACCEPT
PostDown = iptables -D FORWARD -i %i -o $WG_IF -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
PostDown = iptables -t nat -D POSTROUTING -o %i -j MASQUERADE
EOF
    for ip in $CLIENT_IPS; do
        v4 "$ip"
        echo "# $ip: through the Pi, its DNS answered by the Pi"
        echo "PostUp = ip rule del iif $WG_IF from $ip lookup 260 priority 900 2>/dev/null; ip rule add iif $WG_IF from $ip lookup 260 priority 900"
        echo "PostDown = ip rule del iif $WG_IF from $ip lookup 260 priority 900"
        for proto in udp tcp; do
            echo "PostUp = iptables -t nat -I PREROUTING -i $WG_IF -s $ip -p $proto --dport 53 -j DNAT --to-destination 10.99.0.2"
            echo "PostDown = iptables -t nat -D PREROUTING -i $WG_IF -s $ip -p $proto --dport 53 -j DNAT --to-destination 10.99.0.2"
        done
    done
    cat <<EOF

[Peer]
# the Pi
PublicKey = $PI_PUBKEY
AllowedIPs = 0.0.0.0/0
EOF
}

# The kill switch: outside wgpi, so stopping wgpi blocks these clients rather
# than letting them out through the VPS's own IP.
killswitch() {
    up="" down=""
    for ip in $CLIENT_IPS; do
        v4 "$ip"
        up="${up}ip rule del iif $WG_IF from $ip unreachable priority 901 2>/dev/null || true
ip rule add iif $WG_IF from $ip unreachable priority 901
"
        down="${down}ip rule del iif $WG_IF from $ip unreachable priority 901 2>/dev/null || true
"
    done
    for ip in $CLIENT_IP6S; do
        v6 "$ip"
        up="${up}ip6tables -C FORWARD -i $WG_IF -s $ip -j REJECT 2>/dev/null || ip6tables -I FORWARD -i $WG_IF -s $ip -j REJECT
"
        down="${down}ip6tables -D FORWARD -i $WG_IF -s $ip -j REJECT 2>/dev/null || true
"
    done
    put 755 "$KILL" <<EOF
#!/bin/sh
# tunnel-pi-killswitch up|down (written by vps.sh link): Pi-exit clients never
# fall back to the VPS's own IP, and their IPv6 is blocked (the link is IPv4 only).
case "\$1" in
up)
${up};;
down)
${down};;
esac
EOF
    put 644 "$KILL_UNIT" <<EOF
[Unit]
Description=tunnel-pi: Pi-exit clients never fall back to the VPS's IP
After=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$KILL up
ExecStop=$KILL down

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable -q tunnel-pi-killswitch.service
    systemctl restart tunnel-pi-killswitch.service
}

open_port() {
    if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
        ufw allow "$PI_PORT/udp" >/dev/null
        log "ufw: allowed $PI_PORT/udp"
    else
        log "ufw isn't active here: make sure $PI_PORT/udp is open in this VPS's firewall (and the provider's)"
    fi
}

FAILED=0
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; FAILED=1; }
check() { if eval "$2" >/dev/null 2>&1; then pass "$1"; else fail "$1"; fi; }

do_check() {
    if [ ! -f "$WG/wgpi.conf" ]; then fail "wgpi isn't set up (tp link)"; return 1; fi
    check "wgpi is up" "ip link show wgpi"
    t=$(wg show wgpi latest-handshakes 2>/dev/null | awk '{print $2 + 0; exit}')
    if [ "${t:-0}" -gt 0 ] && [ $(( $(date +%s) - t )) -lt 180 ]; then
        pass "wgpi handshake $(( $(date +%s) - t ))s ago"
    else
        fail "wgpi has no recent handshake from the Pi"
    fi
    check "the Pi answers on the link (ping 10.99.0.2)" "ping -c 2 -W 2 10.99.0.2"
    if [ "$EXIT" = tpi ]; then
        check "kill switch is on" "systemctl is-active -q tunnel-pi-killswitch.service"
        check "table 260 sends to the Pi" "ip route show table 260 | grep -q 'default dev wgpi'"
        for ip in $CLIENT_IPS; do
            check "$ip: routed through the Pi (rule 900)" "ip rule show | grep -q '^900:.*from $ip .*lookup 260'"
            check "$ip: kill switch (rule 901)" "ip rule show | grep -q '^901:.*from $ip .*unreachable'"
        done
    else
        for code in $JUMP_CODE $JUMP_LAN_CODE; do
            check "wgt has jump '$code'" "wgt jump list 2>/dev/null | grep -qw -- '$code'"
        done
    fi
    return "$FAILED"
}

do_undo() {
    systemctl disable -q --now wg-quick@wgpi 2>/dev/null || true
    if [ -f "$KILL_UNIT" ]; then
        systemctl disable -q --now tunnel-pi-killswitch.service 2>/dev/null || true
        rm -f "$KILL_UNIT" "$KILL"
        systemctl daemon-reload
    fi
    port=$(sed -n 's/^ListenPort = //p' "$WG/wgpi.conf" 2>/dev/null || true)
    rm -f "$WG"/wgpi.conf "$WG"/wgpi.key "$WG"/wgpi.pub
    if [ -n "$port" ] && command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
        ufw delete allow "$port/udp" >/dev/null || true
    fi
    log "wgpi removed"
}

main "$@" < /dev/null
