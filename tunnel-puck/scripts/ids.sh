# ids.sh: the names and numbers of one puck on its VPS. Sent ahead of puck.sh and
# vps.sh, and read by tpk-ctl.sh, so all three agree.
#
# Names come from the puck's name (PUCK_NAME: its configs/<name>.conf). Addresses,
# the routing table and the rule numbers come from its link's subnet (TPK_LINK),
# a /30 in 10.99.0.0/24 that vps.sh link picks while it's empty: the n-th /30
# gives table 260+n, rules 900+2n and 901+2n, and wgt jump addresses 10.98.n.x.

# link_ids: set the values below (exit 1 on a bad PUCK_NAME or TPK_LINK). With
# TPK_LINK still empty only the names are set; need_link stops a step that needs
# the rest.
link_ids() {
    # Spelled out, not [a-z]: in some locales that range matches capitals too.
    lower=abcdefghijklmnopqrstuvwxyz
    case "${PUCK_NAME:-}" in
        ["$lower"]*) case "$PUCK_NAME" in
            *[!"$lower"0123456789-]*) ids_die "a puck's name is a-z, 0-9 and - (got: $PUCK_NAME)" ;;
        esac ;;
        *) ids_die "a puck's name starts with a lowercase letter (got: ${PUCK_NAME:-nothing})" ;;
    esac
    [ "${#PUCK_NAME}" -le 10 ] \
        || ids_die "a puck's name is at most 10 characters, so wgpk-<name> fits an interface name (got: $PUCK_NAME)"
    LINK_IF="wgpk-$PUCK_NAME"                        # the link's interface on the VPS (on the puck: wgpk)
    KILL_NAME="tunnel-puck-killswitch-$PUCK_NAME"    # tpk routing: the kill switch's script and service on the VPS
    LINK_NET="" VPS_LINK="" PUCK_LINK="" JUMP_NET="" TABLE="" PRIO_ROUTE="" PRIO_KILL=""
    [ -n "${TPK_LINK:-}" ] || return 0
    host="${TPK_LINK#10.99.0.}"; host="${host%/30}"
    case "$TPK_LINK" in 10.99.0.*/30) ;; *) host=x ;; esac
    case "$host" in
        ''|*[!0-9]*) ids_die "TPK_LINK must be a /30 in 10.99.0.0/24, like 10.99.0.8/30 (got: $TPK_LINK)" ;;
    esac
    { [ "$host" -le 252 ] && [ $((host % 4)) -eq 0 ]; } \
        || ids_die "TPK_LINK must start a /30: 10.99.0.0, .4, .8 ... .252 (got: $TPK_LINK)"
    n=$((host / 4))
    LINK_NET="$TPK_LINK"
    VPS_LINK="10.99.0.$((host + 1))"     # the VPS's end of the link
    PUCK_LINK="10.99.0.$((host + 2))"    # the puck's end
    JUMP_NET="10.98.$n"                  # wgt routing: jump servers $JUMP_NET.1/.2 and, LAN mode, .5/.6
    TABLE=$((260 + n))                   # tpk routing: the table that goes to the puck
    PRIO_ROUTE=$((900 + 2 * n))          # tpk routing: the rule sending CLIENT_IPS to TABLE
    PRIO_KILL=$((PRIO_ROUTE + 1))        # tpk routing: the kill switch's rule
}

need_link() { [ -n "$LINK_NET" ] || ids_die "TPK_LINK isn't set yet: tpk-ctl.sh link picks it"; }

ids_die() { printf '%s\n' "$*" >&2; exit 1; }
