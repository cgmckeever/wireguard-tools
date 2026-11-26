#!/bin/sh
# gateway.sh <step>: the gateway's side of tunnel-puck. Runs as root.
#
# scripts/tpk-ctl.sh on your computer streams this file to the gateway over ssh, after the
# settings it needs and ids.sh. To run it on the gateway yourself instead:
#   cat ids.sh gateway.sh | TPK_CONF=/path/to/pucks/<puck>/gateways/<gateway>.conf PUCK_PUBKEY=<the puck's key> sh -s -- <step>
#
# Each puck on this gateway has its own link, wgpk-<name>, with its own subnet,
# routing table and rules (ids.sh); a step touches only its own puck's.
#
# Steps (each is safe to run again):
#   pick    choose the link's subnet (TPK_LINK) and UDP port (PUCK_PORT) where
#           they're empty, and print them (LINK=, PORT=) for tpk-ctl.sh to save.
#           The subnet is free here and isn't one in TPK_LINK_AVOID (the puck's
#           links to other gateways: on the puck they must differ too)
#   link    the link to the puck (PUCK_PUBKEY needed). Prints PUBKEY=<the gateway's
#           public key>. With TPK_ROUTING=tpk it also writes the routing for CLIENT_IPS
#           and the kill switch
#   test    PASS/FAIL for the link and, by TPK_ROUTING, the routing or the wgt jump(s)
#   clients TPK_ROUTING=tpk: each of CLIENT_IPS's last handshake on WG_IF, as
#           "CLIENT <ip> <seconds ago|none|missing> <rx bytes> <tx bytes>" (read-only; for status)
#   undo    remove the link (and TPK_ROUTING=tpk's kill switch). wgt jumps are removed
#           with wgt itself first (see the README's Undo)
#
# TPK_DISABLED=yes (tpk-ctl.sh disable, TPK_ROUTING=tpk): link writes no routing for
# CLIENT_IPS, so only the kill switch applies to them: no internet, never this gateway's IP.
#
# It never changes WG_IF (the clients' interface) or wgt's files. Logs go to stderr;
# stdout is a step's result: LINK=, PORT=, PUBKEY= for tpk-ctl.sh, test's PASS/FAIL.

main() {
    set -eu
    umask 077
    [ "$(id -u)" = 0 ] || die "run as root"
    if [ -n "${TPK_CONF:-}" ]; then . "$TPK_CONF"; fi
    : "${PUCK_NAME:=$(basename "${TPK_CONF:-puck}" .conf | cut -d. -f1)}" "${TPK_LINK:=}" "${PUCK_PORT:=}" "${TPK_LINK_AVOID:=}"
    link_ids
    KILL="/usr/local/sbin/$KILL_NAME" KILL_UNIT="/etc/systemd/system/$KILL_NAME.service"
    : "${TPK_ROUTING:=tpk}" "${PUCK_PUBKEY:=}" "${CLIENT_IPS:=}" "${CLIENT_IP6S:=}" "${WG_SUBNET:=}"
    : "${JUMP_CODE:=}" "${JUMP_LAN_CODE:=}" "${JUMP_LANONLY_CODE:=}" "${WG_IF:=wg0}" "${TPK_DISABLED:=}"
    case "$WG_IF" in ''|*[!A-Za-z0-9_.-]*) die "WG_IF must be an interface name" ;; esac
    case "$TPK_ROUTING" in tpk|wgt) ;; *) die "TPK_ROUTING must be tpk or wgt" ;; esac
    case "${1:-}" in
        pick) pick_link; pick_port ;;
        link) do_link ;;
        test) do_test ;;
        clients) do_clients ;;
        undo) do_undo ;;
        *) die "unknown step '${1:-}' (see the top of gateway.sh)" ;;
    esac
}

WG=/etc/wireguard

# do_clients: each of CLIENT_IPS's peer on WG_IF (the one whose AllowedIPs has
# it) and how long ago it last handshook: none, or missing (no such peer).
do_clients() {
    now=$(date +%s)
    dump=$(wg show "$WG_IF" dump 2>/dev/null | tail -n +2)
    for ip in $CLIENT_IPS; do
        printf '%s\n' "$dump" | awk -F '\t' -v ip="$ip" -v now="$now" '
            { n = split($4, a, ","); for (i = 1; i <= n; i++) if (a[i] == ip "/32") { hs = $5; rx = $6; tx = $7; found = 1 } }
            END { print "CLIENT " ip " " (!found ? "missing" : hs == 0 ? "none" : now - hs) " " (rx + 0) " " (tx + 0) }'
    done
}

log() { printf '==> %s\n' "$*" >&2; }
die() { printf 'gateway.sh: %s\n' "$*" >&2; exit 1; }
need() { for v in "$@"; do eval "x=\${$v:-}"; [ -n "$x" ] || die "$v is not set"; done; }

# put <mode> <path>: write stdin to <path> if it differs; CHANGED=1 when it did, and the
# link restarts.
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
    [ "$TPK_ROUTING" != tpk ] || ip link show "$WG_IF" >/dev/null 2>&1 || die "no interface $WG_IF here: set WG_IF to the WireGuard interface your clients connect to"
    # The link's addresses must be free (or already this link's)
    other=$(link_users "$GATEWAY_LINK" "$PUCK_LINK")
    [ -z "$other" ] || die "$LINK_NET is already used here, by $other: empty TPK_LINK and link picks a free one"
    # PUCK_PORT must be free (or already this link's)
    if [ "$(wg show "$LINK_IF" listen-port 2>/dev/null || true)" != "$PUCK_PORT" ] \
        && ss -Hlun "sport = :$PUCK_PORT" | grep -q .; then
        die "UDP port $PUCK_PORT is already in use (ss -ulpn)"
    fi
    if [ "$TPK_ROUTING" = wgt ]; then
        command -v wgt >/dev/null || die "TPK_ROUTING=wgt needs wgt on this gateway (or use TPK_ROUTING=tpk)"
    fi
}

# link_users <address>...: what else on this gateway has these addresses: an interface,
# or a WireGuard config (up or not). Never this puck's own link.
link_users() {
    for a in "$@"; do
        ip -o -4 addr show | awk -v a="$a/" -v me="$LINK_IF" 'index($4, a) == 1 && $2 != me {print $2}'
        for f in "$WG"/*.conf; do
            [ -e "$f" ] && [ "$f" != "$WG/$LINK_IF.conf" ] || continue
            grep -q "^Address *= *$a/" "$f" && basename "$f" .conf
        done
    done | sort -u | tr '\n' ' ' | sed 's/ $//'
}

# pick_link: TPK_LINK, if empty: this link's own (if it exists), else the first
# /30 in 10.99.0.0/24 that nothing here uses and isn't in TPK_LINK_AVOID.
pick_link() {
    [ -z "$TPK_LINK" ] || return 0
    own=$(sed -n 's|^Address *= *10\.99\.0\.\([0-9]*\)/30$|\1|p' "$WG/$LINK_IF.conf" 2>/dev/null | head -n 1)
    if [ -n "$own" ]; then TPK_LINK="10.99.0.$((own - 1))/30"
    else
        host=0
        while [ "$host" -le 252 ]; do
            case " $TPK_LINK_AVOID " in
                *" 10.99.0.$host/30 "*) ;;    # the puck's link to another gateway
                *) [ -n "$(link_users "10.99.0.$((host + 1))" "10.99.0.$((host + 2))")" ] || break ;;
            esac
            host=$((host + 4))
        done
        [ "$host" -le 252 ] || die "every /30 in 10.99.0.0/24 is in use here (or by the puck's other links)"
        TPK_LINK="10.99.0.$host/30"
        log "picked TPK_LINK=$TPK_LINK"
    fi
    link_ids
    echo "LINK=$TPK_LINK"
}

# pick_port: PUCK_PORT, if empty: this link's own, else the first UDP port from
# 51821 up that nothing listens on and no WireGuard config here names.
pick_port() {
    [ -z "$PUCK_PORT" ] || return 0
    PUCK_PORT=$(sed -n 's/^ListenPort *= *//p' "$WG/$LINK_IF.conf" 2>/dev/null | head -n 1)
    if [ -z "$PUCK_PORT" ]; then
        PUCK_PORT=51821
        while ss -Hlun "sport = :$PUCK_PORT" | grep -q . \
            || grep -qs "^ListenPort *= *$PUCK_PORT\$" "$WG"/*.conf; do
            PUCK_PORT=$((PUCK_PORT + 1))
        done
        log "picked PUCK_PORT=$PUCK_PORT"
    fi
    echo "PORT=$PUCK_PORT"
}

do_link() {
    need PUCK_PUBKEY
    pick_link
    pick_port
    case "$PUCK_PORT" in *[!0-9]*) die "PUCK_PORT must be a number" ;; esac
    preflight
    echo "PUBKEY=$(keypair "$LINK_IF")"

    CHANGED=0
    if [ "$TPK_ROUTING" = wgt ]; then
        put 600 "$WG/$LINK_IF.conf" <<EOF
# tunnel-puck: the link the puck dials (written by gateway.sh link; not wgt's)
[Interface]
Address = $GATEWAY_LINK/30
ListenPort = $PUCK_PORT
MTU = 1420
PostUp = wg set %i private-key $WG/$LINK_IF.key

[Peer]
# the puck
PublicKey = $PUCK_PUBKEY
AllowedIPs = $PUCK_LINK/32
EOF
    else
        conf=$(hand_conf)    # not piped: a die inside must stop the script, not write half a file
        printf '%s\n' "$conf" | put 600 "$WG/$LINK_IF.conf"
        killswitch
    fi
    open_port
    systemctl enable -q "wg-quick@$LINK_IF"
    if [ "$CHANGED" = 1 ]; then systemctl restart "wg-quick@$LINK_IF"; else systemctl start "wg-quick@$LINK_IF"; fi
    [ "$TPK_ROUTING" != tpk ] || prune
    log "$LINK_IF up; it waits for the puck to dial in"
}

# listed <address> <list>: is it in the list (a /32 or /128 written there or not)?
listed() { for x in $2; do [ "${x%/*}" = "$1" ] && return 0; done; return 1; }

# prune: remove the rules of clients taken out of CLIENT_IPS / CLIENT_IP6S. A
# restart can't: it removes what the new files list, and they no longer list them.
prune() {
    ip rule show | awk -v ifc="$WG_IF" -v r="$PRIO_ROUTE:" -v k="$PRIO_KILL:" '($1 == r || $1 == k) && $2 == "from" {
        for (i = 3; i < NF; i++) if ($i == "iif" && $(i + 1) == ifc) { sub(":", "", $1); print $1, $3 } }' |
    while read -r prio ip; do
        listed "$ip" "$CLIENT_IPS" && continue
        ip rule del priority "$prio" from "$ip" iif "$WG_IF" && log "removed rule $prio for $ip (no longer in CLIENT_IPS)"
    done
    iptables -t nat -S PREROUTING | grep -- "-i $WG_IF .*--to-destination $PUCK_LINK\$" |
    while read -r rule; do
        ip=$(printf '%s\n' "$rule" | sed -n 's|.* -s \([0-9.]*\)/32 .*|\1|p')
        [ -n "$ip" ] && ! listed "$ip" "$CLIENT_IPS" || continue
        # shellcheck disable=SC2086
        iptables -t nat $(printf '%s\n' "$rule" | sed 's/^-A /-D /') && log "removed the DNS redirect for $ip"
    done
    ip6tables -S FORWARD 2>/dev/null | grep -- "-i $WG_IF -j REJECT" |
    while read -r rule; do
        ip=$(printf '%s\n' "$rule" | sed -n 's|.* -s \([0-9a-fA-F:]*\)/128 .*|\1|p')
        [ -n "$ip" ] && ! listed "$ip" "$CLIENT_IP6S" || continue
        # shellcheck disable=SC2086
        ip6tables $(printf '%s\n' "$rule" | sed 's/^-A /-D /') && log "removed the IPv6 block for $ip"
    done
}

# The TPK_ROUTING=tpk link config: route CLIENT_IPS through the puck, NAT onto the link, DNS to the puck.
hand_conf() {
    if [ -z "$WG_SUBNET" ]; then
        # Every IPv4 subnet WG_IF is on (its kernel routes), so traffic between
        # clients stays on WG_IF
        WG_SUBNET=$(ip -4 route show dev "$WG_IF" proto kernel scope link 2>/dev/null | awk '{printf "%s ", $1}')
        [ -n "$WG_SUBNET" ] || die "WG_SUBNET is not set and $WG_IF has no IPv4 subnet to read"
    fi
    for net in $WG_SUBNET; do v4 "$net"; done
    cat <<EOF
# tunnel-puck: the link the puck dials, and the clients routed through it (written by gateway.sh link)
[Interface]
Address = $GATEWAY_LINK/30
ListenPort = $PUCK_PORT
MTU = 1420
# Table = off: no routes from AllowedIPs; the rules below choose who uses the link
Table = off
PostUp = wg set %i private-key $WG/$LINK_IF.key
# Table $TABLE: the puck for everything, except the gateway's own clients, which stay on $WG_IF
$(for net in $WG_SUBNET; do echo "PostUp = ip route replace $net dev $WG_IF table $TABLE"; done)
PostUp = ip route replace default dev %i table $TABLE
PostDown = ip route flush table $TABLE
# Forward into the link and NAT onto it (the puck sees only $GATEWAY_LINK)
PostUp = iptables -I FORWARD -i $WG_IF -o %i -j ACCEPT
PostUp = iptables -I FORWARD -i %i -o $WG_IF -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
PostUp = iptables -t nat -A POSTROUTING -o %i -j MASQUERADE
PostDown = iptables -D FORWARD -i $WG_IF -o %i -j ACCEPT
PostDown = iptables -D FORWARD -i %i -o $WG_IF -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
PostDown = iptables -t nat -D POSTROUTING -o %i -j MASQUERADE
EOF
    # Disabled (tpk-ctl.sh disable): no client is routed to the puck, so the kill
    # switch blocks them all; the link itself stays up.
    [ "$TPK_DISABLED" != yes ] || echo "# disabled: CLIENT_IPS ($CLIENT_IPS) get no routing here, so the kill switch blocks them"
    [ "$TPK_DISABLED" = yes ] || for ip in $CLIENT_IPS; do
        v4 "$ip"
        echo "# $ip: through the puck, its DNS answered by the puck"
        echo "PostUp = ip rule del iif $WG_IF from $ip lookup $TABLE priority $PRIO_ROUTE 2>/dev/null; ip rule add iif $WG_IF from $ip lookup $TABLE priority $PRIO_ROUTE"
        echo "PostDown = ip rule del iif $WG_IF from $ip lookup $TABLE priority $PRIO_ROUTE"
        for proto in udp tcp; do
            echo "PostUp = iptables -t nat -I PREROUTING -i $WG_IF -s $ip -p $proto --dport 53 -j DNAT --to-destination $PUCK_LINK"
            echo "PostDown = iptables -t nat -D PREROUTING -i $WG_IF -s $ip -p $proto --dport 53 -j DNAT --to-destination $PUCK_LINK"
        done
    done
    cat <<EOF

[Peer]
# the puck
PublicKey = $PUCK_PUBKEY
AllowedIPs = 0.0.0.0/0
EOF
}

# The kill switch: outside the link, so stopping it blocks these clients rather
# than letting them out through the gateway's own IP.
killswitch() {
    up="" down=""
    for ip in $CLIENT_IPS; do
        v4 "$ip"
        up="${up}ip rule del iif $WG_IF from $ip unreachable priority $PRIO_KILL 2>/dev/null || true
ip rule add iif $WG_IF from $ip unreachable priority $PRIO_KILL
"
        down="${down}ip rule del iif $WG_IF from $ip unreachable priority $PRIO_KILL 2>/dev/null || true
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
# $KILL_NAME up|down (written by gateway.sh link, for the puck $PUCK_NAME): puck-exit clients never
# fall back to the gateway's own IP, and their IPv6 is blocked (the link is IPv4 only).
case "\$1" in
up)
${up};;
down)
${down};;
esac
EOF
    put 644 "$KILL_UNIT" <<EOF
[Unit]
Description=tunnel-puck: puck-exit clients never fall back to the gateway's IP
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
    systemctl enable -q "$KILL_NAME.service"
    systemctl restart "$KILL_NAME.service"
}

ufw_active() { command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; }

open_port() {
    if ufw_active; then
        ufw allow "$PUCK_PORT/udp" >/dev/null
        log "ufw: allowed $PUCK_PORT/udp"
    else
        log "ufw isn't active here: make sure $PUCK_PORT/udp is open in this gateway's firewall (and the provider's)"
    fi
}

FAILED=0
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; FAILED=1; }
check() { if eval "$2" >/dev/null 2>&1; then pass "$1"; else fail "$1"; fi; }

do_test() {
    if [ ! -f "$WG/$LINK_IF.conf" ]; then fail "$LINK_IF isn't set up (tpk-ctl.sh link)"; return 1; fi
    if [ -z "$LINK_NET" ]; then fail "TPK_LINK is empty in this puck's settings (tpk-ctl.sh link fills it in)"; return 1; fi
    check "$LINK_IF is up" "ip link show $LINK_IF"
    t=$(wg show "$LINK_IF" latest-handshakes 2>/dev/null | awk '{print $2 + 0; exit}')
    if [ "${t:-0}" -gt 0 ] && [ $(( $(date +%s) - t )) -lt 180 ]; then
        pass "$LINK_IF handshake $(( $(date +%s) - t ))s ago"
    else
        fail "$LINK_IF has no recent handshake from the puck"
    fi
    check "the puck answers on the link (ping $PUCK_LINK)" "ping -c 2 -W 2 $PUCK_LINK"
    if [ "$TPK_ROUTING" = tpk ]; then
        check "kill switch is on" "systemctl is-active -q $KILL_NAME.service"
        check "table $TABLE sends to the puck" "ip route show table $TABLE | grep -q 'default dev $LINK_IF'"
        [ "$TPK_DISABLED" != yes ] || printf 'INFO  disabled: CLIENT_IPS get no routing to the puck, only the kill switch (tpk-ctl.sh enable)\n'
        for ip in $CLIENT_IPS; do
            if [ "$TPK_DISABLED" = yes ]; then
                check "$ip: not routed through the puck (disabled)" "! ip rule show | grep -q '^$PRIO_ROUTE:.*from $ip .*lookup $TABLE'"
            else
                check "$ip: routed through the puck (rule $PRIO_ROUTE)" "ip rule show | grep -q '^$PRIO_ROUTE:.*from $ip .*lookup $TABLE'"
            fi
            check "$ip: kill switch (rule $PRIO_KILL)" "ip rule show | grep -q '^$PRIO_KILL:.*from $ip .*unreachable'"
        done
    else
        for code in $JUMP_CODE $JUMP_LAN_CODE $JUMP_LANONLY_CODE; do
            check "wgt has jump '$code'" "wgt jump list 2>/dev/null | grep -qw -- '$code'"
        done
    fi
    return "$FAILED"
}

do_undo() {
    systemctl disable -q --now "wg-quick@$LINK_IF" 2>/dev/null || true
    if [ -f "$KILL_UNIT" ]; then
        systemctl disable -q --now "$KILL_NAME.service" 2>/dev/null || true
        rm -f "$KILL_UNIT" "$KILL"
        systemctl daemon-reload
    fi
    port=$(sed -n 's/^ListenPort = //p' "$WG/$LINK_IF.conf" 2>/dev/null || true)
    rm -f "$WG/$LINK_IF.conf" "$WG/$LINK_IF.key" "$WG/$LINK_IF.pub"
    if [ -n "$port" ] && ufw_active; then
        ufw delete allow "$port/udp" >/dev/null || true
    fi
    log "$LINK_IF removed"
}

main "$@" < /dev/null
