# ids.sh: the names and numbers of one tunnel-pi on its VPS. Sent ahead of pi.sh
# and vps.sh, and read by tpi-ctl.sh, so all three agree.
#
# Names come from the Pi's name (PI_NAME, its configs/<name>.conf). Addresses,
# the routing table and rule numbers come from its link subnet (TPI_LINK), a /30
# in 10.99.0.0/24 that vps.sh link picks when it's empty: the n-th /30 gives
# table 260+n, rules 900+2n and 901+2n, and wgt jump addresses 10.98.n.x.

# link_ids: set the values below (exit 1 on a bad PI_NAME or TPI_LINK). With
# TPI_LINK still empty, only the names are set; need_link stops a step that
# needs the rest.
link_ids() {
    case "${PI_NAME:-}" in
        [a-z]*) case "$PI_NAME" in *[!a-z0-9-]*) ids_die "a Pi's name is a-z, 0-9 and - (got: $PI_NAME)" ;; esac ;;
        *) ids_die "a Pi's name starts with a letter (got: ${PI_NAME:-nothing})" ;;
    esac
    [ "${#PI_NAME}" -le 10 ] || ids_die "a Pi's name is at most 10 characters (got: $PI_NAME): wgpi-<name> must fit an interface name"
    LINK_IF="wgpi-$PI_NAME"                     # the link's interface on the VPS (on the Pi: wgpi)
    KILL_NAME="tunnel-pi-killswitch-$PI_NAME"   # tpi: the kill switch's script and service on the VPS
    LINK_NET="" VPS_LINK="" PI_LINK="" JUMP_NET="" TABLE="" PRIO_ROUTE="" PRIO_KILL=""
    [ -n "${TPI_LINK:-}" ] || return 0
    host="${TPI_LINK#10.99.0.}"; host="${host%/30}"
    case "$TPI_LINK" in 10.99.0.*/30) ;; *) host=x ;; esac
    case "$host" in ''|*[!0-9]*) ids_die "TPI_LINK must be a /30 in 10.99.0.0/24, like 10.99.0.8/30 (got: $TPI_LINK)" ;; esac
    { [ "$host" -le 252 ] && [ $((host % 4)) -eq 0 ]; } \
        || ids_die "TPI_LINK must start a /30: 10.99.0.0, .4, .8 ... .252 (got: $TPI_LINK)"
    n=$((host / 4))
    LINK_NET="$TPI_LINK"
    VPS_LINK="10.99.0.$((host + 1))"            # the VPS's end
    PI_LINK="10.99.0.$((host + 2))"             # the Pi's end
    JUMP_NET="10.98.$n"                         # wgt: jump servers $JUMP_NET.1/.2 and, LAN mode, .5/.6
    TABLE=$((260 + n))                          # tpi: the routing table to the Pi
    PRIO_ROUTE=$((900 + 2 * n))                 # tpi: the rule sending CLIENT_IPS to TABLE
    PRIO_KILL=$((PRIO_ROUTE + 1))               # tpi: the kill switch rule
}

need_link() { [ -n "$LINK_NET" ] || ids_die "TPI_LINK isn't set yet: tpi-ctl.sh link picks it"; }

ids_die() { printf '%s\n' "$*" >&2; exit 1; }
