# ids.sh: the names and numbers of one puck's link to one gateway. Sent ahead of
# puck.sh and gateway.sh, and read by tpk-ctl.sh, so all three agree.
#
# The gateway's names come from the puck's name (PUCK_NAME: its fleet/pucks/<name>/ folder).
# Everything else comes from the link's subnet (TPK_LINK, in the puck's
# fleet/pucks/<name>/gateways/<gateway>.conf), a /30 in 10.99.0.0/24 that gateway.sh pick chooses
# while it's empty, free on that gateway and among the puck's other links. The n-th
# /30 gives the puck's interfaces wgpk<n>, wgrtun<n>, wgrtunlan<n> and wgrtunlo<n>
# (the rtuns, on ports 51822+3n, 51823+3n and 51824+3n), wgt jump addresses
# 10.98.n.x, and on the gateway table 260+n and rules 900+2n and 901+2n.

# link_ids: set the values below (exit 1 on a bad PUCK_NAME or TPK_LINK). With
# TPK_LINK still empty only the names are set; need_link stops a step that needs
# the rest.
link_ids() {
    name_ok puck "${PUCK_NAME:-}"
    [ "${#PUCK_NAME}" -le 10 ] \
        || ids_die "a puck's name is at most 10 characters, so wgpk-<name> fits an interface name (got: $PUCK_NAME)"
    LINK_IF="wgpk-$PUCK_NAME"                        # the link's interface on the gateway (on the puck: wgpk<n>)
    KILL_NAME="tunnel-puck-killswitch-$PUCK_NAME"    # tpk routing: the kill switch's script and service on the gateway
    LINK_NET="" GATEWAY_LINK="" PUCK_LINK="" JUMP_NET="" TABLE="" PRIO_ROUTE="" PRIO_KILL=""
    LINK_N="" PK_IF="" RTUN_IF="" RTUN_LAN_IF="" RTUN_LO_IF="" RTUN_PORT="" RTUN_LAN_PORT="" RTUN_LO_PORT=""
    [ -n "${TPK_LINK:-}" ] || return 0
    host="${TPK_LINK#10.99.0.}"; host="${host%/30}"
    case "$TPK_LINK" in 10.99.0.*/30) ;; *) host=x ;; esac
    case "$host" in
        ''|*[!0-9]*) ids_die "TPK_LINK must be a /30 in 10.99.0.0/24, like 10.99.0.8/30 (got: $TPK_LINK)" ;;
    esac
    { [ "$host" -le 252 ] && [ $((host % 4)) -eq 0 ]; } \
        || ids_die "TPK_LINK must start a /30: 10.99.0.0, .4, .8 ... .252 (got: $TPK_LINK)"
    n=$((host / 4))
    LINK_N=$n
    PK_IF="wgpk$n"                       # the link's interface on the puck
    RTUN_IF="wgrtun$n"                   # wgt routing: the reverse tunnel the gateway reaches in through (wgt's jump)
    RTUN_LAN_IF="wgrtunlan$n"            # ... its egress+lan twin (the LAN jump)
    RTUN_LO_IF="wgrtunlo$n"              # ... and its lan one (the LAN-only jump)
    RTUN_PORT=$((51822 + 3 * n))         # their ports on the puck, inside the link
    RTUN_LAN_PORT=$((RTUN_PORT + 1))
    RTUN_LO_PORT=$((RTUN_PORT + 2))
    LINK_NET="$TPK_LINK"
    GATEWAY_LINK="10.99.0.$((host + 1))"     # the gateway's end of the link
    PUCK_LINK="10.99.0.$((host + 2))"    # the puck's end
    JUMP_NET="10.98.$n"                  # wgt routing: jump servers $JUMP_NET.1/.2, the LAN jump .5/.6, the LAN-only .9/.10
    TABLE=$((260 + n))                   # tpk routing: the table that goes to the puck
    PRIO_ROUTE=$((900 + 2 * n))          # tpk routing: the rule sending CLIENT_IPS to TABLE
    PRIO_KILL=$((PRIO_ROUTE + 1))        # tpk routing: the kill switch's rule
}

need_link() { [ -n "$LINK_NET" ] || ids_die "TPK_LINK isn't set yet: tpk-ctl.sh link picks it"; }

# gateway_name_ok <name>: a gateway's name, as in fleet/pucks/<puck>/gateways/<gateway>.conf.
gateway_name_ok() { name_ok gateway "$1"; }

# name_ok puck|gateway <name>: a-z, 0-9 and -, starting with a letter.
name_ok() {
    # Spelled out, not [a-z]: in some locales that range matches capitals too.
    lower=abcdefghijklmnopqrstuvwxyz
    case "$2" in
        ["$lower"]*) case "$2" in *[!"$lower"0123456789-]*) ids_die "a $1's name is a-z, 0-9 and - (got: $2)" ;; esac ;;
        *) ids_die "a $1's name starts with a lowercase letter (got: ${2:-nothing})" ;;
    esac
}

ids_die() { printf '%s\n' "$*" >&2; exit 1; }
