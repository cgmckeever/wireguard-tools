# shellcheck shell=bash
# shellcheck disable=SC2034  # globals here are used by wg-setup.sh / wg-client.sh
# lib/common.sh — shared by wg-setup.sh and wg-client.sh. Not run directly.
#
# Exit modes:
#   warp    client traffic exits through Cloudflare WARP. The server's main
#           routing table is never touched (warp.conf uses Table = off); only
#           packets from the WireGuard subnet are policy-routed into RT_TABLE,
#           whose default is the warp interface, with a blackhole fallback so
#           clients fail closed (no leak via the VPS's own IP) if warp drops.
#   direct  client traffic is NATed out the server's public interface.
#
# IPv6: clients also get an address in WG_SUBNET6 (a ULA /64, random by
# default) and their IPv6 is NATed out the same way: through WARP's own IPv6
# in warp mode (which works on IPv4-only hosts), or out the public interface
# in direct mode when the host has IPv6. WG_SUBNET6=off disables it.
#
# Every routing/NAT rule lives in wg0.conf PostUp/PostDown, generated from one
# list (hook_cmds), so PostDown and rollback exactly invert PostUp. The one
# exception: warp.conf's PostUp also re-adds RT_TABLE's default route
# (warp_route), which goes when warp does, so a warp restart that doesn't
# restart wg0 can't leave clients blackholed.

# --- Config ---
# Saved in the state file by `wg-setup.sh direct/warp`, so later runs and
# wg-client.sh use the server's real settings.
# Precedence: flag > environment variable > saved value > default.
CONFIG_KEYS=(EXIT SERVER_NAME WG_SUBNET WG_SUBNET6 WG_PORT WG_MTU WARP_MTU WG_DNS WG_IF WARP_IF RT_TABLE CLIENT_ALLOWED_IPS)
EXIT=""                 # set by wg-setup.sh direct/warp, else the saved mode
SERVER_NAME="${SERVER_NAME:-}"
WG_SUBNET="${WG_SUBNET:-}"
WG_SUBNET6="${WG_SUBNET6:-}"   # ULA /64, or "off"; setup picks a random one
WG_PORT="${WG_PORT:-}"
WG_MTU="${WG_MTU:-}"
WARP_MTU="${WARP_MTU:-}"
WG_DNS="${WG_DNS:-}"
WG_IF="${WG_IF:-}"
WARP_IF="${WARP_IF:-}"
RT_TABLE="${RT_TABLE:-}"
CLIENT_ALLOWED_IPS="${CLIENT_ALLOWED_IPS:-}"
ALLOWED_IPS_GIVEN=0     # 1 if --allowed-ips or CLIENT_ALLOWED_IPS was given
if [ -n "$CLIENT_ALLOWED_IPS" ]; then ALLOWED_IPS_GIVEN=1; fi
ENDPOINT="${ENDPOINT:-}"
WGCF_VERSION="${WGCF_VERSION:-latest}"
WGCF_SHA256="${WGCF_SHA256:-}"
RULE_PRIO=10000
CALLER_TMPDIR="${TMPDIR:-}"   # init points TMPDIR at a private temp dir

# --- Fixed paths and constants ---
WG_DIR=/etc/wireguard
WGCF_DIR="$WG_DIR/wgcf"
WGCF_BIN=/usr/local/bin/wgcf
STATE="$WG_DIR/wg-tools.state"
BACKUP_MAP="$WG_DIR/wg-tools.backups"
CLIENT_DB="$WG_DIR/clients.db"
PSK_DIR="$WG_DIR/psk"
CLIENT_DIR=/root/clients
SERVER_KEY="$WG_DIR/server.key"
SERVER_PUB="$WG_DIR/server.pub"
SYSCTL_FILE=/etc/sysctl.d/99-wg.conf
LOCK_FILE=/run/wg-tools.lock
TRACE_URL=https://www.cloudflare.com/cdn-cgi/trace
BLACKHOLE_METRIC=4096
MARKER="# Managed by wireguard-tools"
TEMPLATE_DIR="$(dirname "$SELF")/templates"
BACKUP_DIR="$(dirname "$SELF")/backup"   # untracked (.gitignore); mode 700

# --- Flags ---
DRY_RUN=0
YES=0
FORCE=0
PURGE=0
QR=0
ARGS=()

# --- Runtime ---
STEP="startup"
WROTE=0                 # set by write_file: 1 if the file changed
CLEANUP_ON_ERR=0        # 1 while wg0's PostUp rules may be half-applied

# --- Output helpers ---

log()  { printf '==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Announce a step and how to undo it (printed before any change is made).
step() {
    STEP="$1"
    log "$1"
    if [ -n "${2:-}" ]; then info "undo: $2"; fi
}

# Run a command, or just print it under --dry-run.
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '    [dry-run] %s\n' "$*"
    else
        "$@"
    fi
}

# Ask before a network change; --yes and --dry-run skip the prompt.
confirm() {
    if [ "$DRY_RUN" -eq 1 ] || [ "$YES" -eq 1 ]; then return 0; fi
    have_tty || die "no terminal to confirm \"$1\"; re-run with --yes"
    local reply=""
    read -r -p "    $1 [y/N] " reply </dev/tty || true
    [[ $reply =~ ^[Yy] ]] || die "aborted at: $STEP (nothing further changed; --yes skips prompts)"
}

# ERR trap: report where we died and undo half-applied wg0 rules.
on_err() {
    local rc="$1" line="$2" cmd="$3"
    trap - ERR
    printf '\nERROR: step "%s" failed (%s line %s, exit %s): %s\n' "$STEP" "$PROG" "$line" "$rc" "$cmd" >&2
    if [ "$CLEANUP_ON_ERR" -eq 1 ]; then
        printf 'Removing partially applied %s routing/NAT rules...\n' "$WG_IF" >&2
        run_hooks down >&2 || true
    fi
    printf 'State is safe to re-run; "wg-setup.sh status" shows what is in place.\n' >&2
    exit "$rc"
}

# Strip key values so dry-run diffs and `show` never print secrets.
redact() { sed -E 's/^([[:space:]]*(PrivateKey|PresharedKey)[[:space:]]*=[[:space:]]*).*/\1<redacted>/'; }

# --- Argument parsing and validation ---

# Map a config flag to its variable.
set_opt() {
    case "$1" in
        --name)        SERVER_NAME="$2" ;;
        --subnet)      WG_SUBNET="$2" ;;
        --subnet6)     WG_SUBNET6="$2" ;;
        --port)        WG_PORT="$2" ;;
        --mtu)         WG_MTU="$2" ;;
        --warp-mtu)    WARP_MTU="$2" ;;
        --dns)         WG_DNS="$2" ;;
        --endpoint)    ENDPOINT="$2" ;;
        --wg-if)       WG_IF="$2" ;;
        --warp-if)     WARP_IF="$2" ;;
        --table)       RT_TABLE="$2" ;;
        --allowed-ips) CLIENT_ALLOWED_IPS="$2"; ALLOWED_IPS_GIVEN=1 ;;
        *) die "unknown option: $1 (see: $PROG help)" ;;
    esac
}

# Parse flags anywhere on the command line; positionals go to ARGS.
parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run) DRY_RUN=1 ;;
            --yes|-y)  YES=1 ;;
            --force)   FORCE=1 ;;
            --purge)   PURGE=1 ;;
            --qr)      QR=1 ;;
            -h|--help) ARGS=(help); return 0 ;;
            --*=*)     set_opt "${1%%=*}" "${1#*=}" ;;
            --name|--subnet|--subnet6|--port|--mtu|--warp-mtu|--dns|--endpoint|--wg-if|--warp-if|--table|--allowed-ips)
                [ $# -ge 2 ] || die "$1 needs a value"
                set_opt "$1" "$2"
                shift
                ;;
            -*) die "unknown option: $1 (see: $PROG help)" ;;
            *)  ARGS+=("$1") ;;
        esac
        shift
    done
}

# Fill unset config from defaults (MTU default depends on the exit mode).
apply_defaults() {
    : "${EXIT:=warp}" "${WG_SUBNET:=10.8.0.0/24}" "${WG_PORT:=51820}" "${WARP_MTU:=1280}"
    : "${WG_DNS:=1.1.1.1}" "${WG_IF:=wg0}" "${WARP_IF:=warp}" "${RT_TABLE:=100}"
    : "${CLIENT_ALLOWED_IPS:=0.0.0.0/0, ::/0}"
    if [ -z "$SERVER_NAME" ]; then
        SERVER_NAME=$(hostname -s 2>/dev/null | tr -c 'A-Za-z0-9_\n-' - | cut -c1-32)
        : "${SERVER_NAME:=wg}"
    fi
    if [ -z "$WG_MTU" ]; then
        if [ "$EXIT" = warp ]; then WG_MTU=1280; else WG_MTU=1420; fi
    fi
}

# Fill unset config from the state file, then defaults.
load_config() {
    local k v
    SAVED_EXIT=$(state_get EXIT)
    SAVED_SUBNET=$(state_get WG_SUBNET)
    SAVED_SERVER_NAME=$(state_get SERVER_NAME)
    # Switching modes in place would mix two rule sets; rollback frees the mode.
    if [ -n "$EXIT" ] && [ -n "$SAVED_EXIT" ] && [ "$EXIT" != "$SAVED_EXIT" ]; then
        die "this server is set up for '$SAVED_EXIT'; run 'wg-setup.sh rollback' before setting it up for '$EXIT'"
    fi
    for k in "${CONFIG_KEYS[@]}"; do
        if [ -z "${!k}" ]; then
            v=$(state_get "$k")
            if [ -n "$v" ]; then printf -v "$k" '%s' "$v"; fi
        fi
    done
    apply_defaults
}

# Persist the current config for later runs and wg-client.sh.
save_config() {
    local k
    for k in "${CONFIG_KEYS[@]}"; do
        if [ "$(state_get "$k")" != "${!k}" ]; then state_set "$k" "${!k}"; fi
    done
}

# IPv4 dotted quad -> integer, and back.
ip2int() {
    local a b c d
    IFS=. read -r a b c d <<<"$1"
    echo $(( (a << 24) | (b << 16) | (c << 8) | d ))
}
int2ip() {
    local n="$1"
    echo "$(( (n >> 24) & 255 )).$(( (n >> 16) & 255 )).$(( (n >> 8) & 255 )).$(( n & 255 ))"
}

# True if we can prompt on the terminal.
have_tty() { { : </dev/tty; } 2>/dev/null; }

# Lowercase a "<groups>::/64" prefix and drop leading zeros, as ip(8) prints it.
canon_subnet6() {
    local p="${1,,}" g out=""
    [[ $p =~ ^([0-9a-f]{1,4}:){0,3}[0-9a-f]{1,4}::/64$ ]] || return 1
    p="${p%::/64}"
    for g in ${p//:/ }; do
        g=$(( 16#$g ))
        out="$out${out:+:}$(printf '%x' "$g")"
    done
    echo "$out::/64"
}

# A new random ULA /64 (RFC 4193): fdXX:XXXX:XXXX::/64.
random_subnet6() {
    local h
    h=$(od -An -N5 -tx1 /dev/urandom | tr -d ' \n')
    canon_subnet6 "fd${h:0:2}:${h:2:4}:${h:6:4}::/64"
}

# The client IPv6 matching an IPv4 address: same host number in WG_SUBNET6.
ip6_of() { echo "$P6::$(printf '%x' $(( $(ip2int "$1") - NET_INT )))"; }

# A client's addresses as "ip/32" or "ip/32,ip6/128" (comma list, no spaces).
client_addrs() {
    if [ "$IPV6" -eq 1 ]; then echo "$1/32,$(ip6_of "$1")/128"; else echo "$1/32"; fi
}

# True if $1 is an integer in [$2, $3].
in_range() { [[ $1 =~ ^[0-9]+$ ]] && [ "$1" -ge "$2" ] && [ "$1" -le "$3" ]; }

# Check config values and derive subnet addresses and per-interface paths.
finalize_config() {
    local ifre='^[A-Za-z0-9_-]{1,15}$' net octet mask
    case "$EXIT" in warp|direct) ;; *) die "bad saved mode: $EXIT (expected warp or direct)" ;; esac
    [[ $SERVER_NAME =~ ^[A-Za-z0-9_-]{1,32}$ ]] || die "server name must be 1-32 of [A-Za-z0-9_-]: '$SERVER_NAME'"
    [[ $WG_IF =~ $ifre ]] || die "bad interface name: $WG_IF"
    [[ $WARP_IF =~ $ifre ]] || die "bad interface name: $WARP_IF"
    [ "$WG_IF" != "$WARP_IF" ] || die "WG_IF and WARP_IF must differ"
    in_range "$WG_PORT" 1 65535 || die "bad port: $WG_PORT"
    in_range "$WG_MTU" 1280 9000 || die "bad MTU (1280-9000): $WG_MTU"
    in_range "$WARP_MTU" 1280 9000 || die "bad WARP MTU (1280-9000): $WARP_MTU"
    if [ "$EXIT" = warp ] && [ "$WG_MTU" -gt "$WARP_MTU" ]; then
        die "WG_MTU ($WG_MTU) must not exceed WARP_MTU ($WARP_MTU)"
    fi
    in_range "$RT_TABLE" 1 252 || die "bad table (1-252): $RT_TABLE"
    [[ $WG_SUBNET =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/([0-9]{1,2})$ ]] || die "bad subnet: $WG_SUBNET"

    net="${WG_SUBNET%/*}"
    PFX="${WG_SUBNET#*/}"
    for octet in ${net//./ }; do
        [ "$octet" -le 255 ] || die "bad subnet: $WG_SUBNET"
    done
    in_range "$PFX" 16 30 || die "subnet prefix must be /16 to /30: $WG_SUBNET"
    mask=$(( (0xFFFFFFFF << (32 - PFX)) & 0xFFFFFFFF ))
    NET_INT=$(( $(ip2int "$net") & mask ))
    [ "$(int2ip "$NET_INT")" = "$net" ] || die "$WG_SUBNET is not a network address (did you mean $(int2ip "$NET_INT")/$PFX?)"
    SERVER_IP=$(int2ip $(( NET_INT + 1 )))
    FIRST_HOST=$(( NET_INT + 2 ))
    LAST_HOST=$(( NET_INT + (1 << (32 - PFX)) - 2 ))

    # IPv6 is on when a /64 is set (setup picks one) and there's a way out:
    # WARP's IPv6 (warp), or the host's own (direct).
    IPV6=0
    case "$WG_SUBNET6" in
        ""|off) ;;
        *)
            WG_SUBNET6=$(canon_subnet6 "$WG_SUBNET6") || die "bad --subnet6 (want a /64 like fd12:3456:789a::/64, or off): $WG_SUBNET6"
            P6="${WG_SUBNET6%::/64}"
            if [ "$EXIT" = warp ] && kernel_ipv6; then IPV6=1
            elif [ "$EXIT" = direct ] && has_ipv6; then IPV6=1; fi
            ;;
    esac

    WARP_CONF="$WG_DIR/$WARP_IF.conf"
    WG_CONF="$WG_DIR/$WG_IF.conf"
    DROPIN_DIR="/etc/systemd/system/wg-quick@$WG_IF.service.d"
    DROPIN="$DROPIN_DIR/10-after-$WARP_IF.conf"
}

# Root, OS and architecture checks; refuse anything unsupported.
preflight() {
    [ "$(id -u)" -eq 0 ] || die "must run as root"
    [ -r /etc/os-release ] || die "cannot read /etc/os-release; unsupported OS"
    local id ver major
    # shellcheck source=/dev/null
    id=$(. /etc/os-release && echo "${ID:-}")
    # shellcheck source=/dev/null
    ver=$(. /etc/os-release && echo "${VERSION_ID:-}")
    major="${ver%%.*}"
    [[ $major =~ ^[0-9]+$ ]] || die "unsupported OS: $id ${ver:-<no version>} (need Debian 11+ or Ubuntu 20.04+)"
    case "$id" in
        debian) [ "$major" -ge 11 ] || die "unsupported Debian $ver (need 11+)" ;;
        ubuntu) [ "$major" -ge 20 ] || die "unsupported Ubuntu $ver (need 20.04+)" ;;
        *) die "unsupported OS: $id $ver (need Debian 11+ or Ubuntu 20.04+)" ;;
    esac
    ARCH=$(dpkg --print-architecture)
    case "$ARCH" in
        amd64|arm64) ;;
        *) die "unsupported architecture: $ARCH (need amd64 or arm64)" ;;
    esac
}

# Common startup: parse flags, checks, config, temp dir, traps.
# Leaves the subcommand and its args in ARGS. Exits after `help`.
init() {
    parse_args "$@"
    if declare -F command_mode >/dev/null; then EXIT=$(command_mode "${ARGS[0]:-}"); fi
    if [ "${ARGS[0]:-}" = help ] || [ ${#ARGS[@]} -eq 0 ]; then
        apply_defaults
        usage
        [ ${#ARGS[@]} -gt 0 ] || exit 1
        exit 0
    fi
    preflight
    load_config
    finalize_config
    TMPD=$(mktemp -d)
    export TMPDIR="$TMPD"
    trap 'rm -rf "$TMPD"' EXIT
    trap 'on_err "$?" "$LINENO" "$BASH_COMMAND"' ERR
    umask 077   # keys and configs are private by default; public files set their mode
    if [ "$DRY_RUN" -eq 1 ]; then
        log "DRY RUN: nothing will be changed"
    else
        install -d -m 700 "$WG_DIR"
    fi
}

# Serialize mutating commands (shared by both scripts).
take_lock() {
    exec 9>"$LOCK_FILE"
    flock -n 9 || die "another wireguard-tools command is running"
}

# --- State, backups and file writes ---

# Read/write/remove KEY=value pairs in the state file.
state_get() {
    [ -f "$STATE" ] || return 0
    sed -n "s/^$1=//p" "$STATE" | tail -n 1
}
state_set() {
    if [ "$DRY_RUN" -eq 1 ]; then
        info "[dry-run] would record $1=$2 in $STATE"
        return 0
    fi
    local tmp
    tmp=$(mktemp)
    if [ -f "$STATE" ]; then grep -v "^$1=" "$STATE" >"$tmp" || true; fi
    printf '%s=%s\n' "$1" "$2" >>"$tmp"
    install -m 600 -D "$tmp" "$STATE"
}
state_unset() {
    [ -f "$STATE" ] || return 0
    if [ "$DRY_RUN" -eq 1 ]; then
        info "[dry-run] would clear $1 in $STATE"
        return 0
    fi
    local tmp
    tmp=$(mktemp)
    grep -v "^$1=" "$STATE" >"$tmp" || true
    install -m 600 "$tmp" "$STATE"
}

# Copy a file to a timestamped .bak before changing it; remember the first
# (original) backup per file, or NEW if we created it, for rollback.
backup_file() {
    local f="$1" bak="NEW"
    if [ -e "$f" ]; then
        bak="$f.$(date +%Y%m%d-%H%M%S).bak"
        while [ -e "$bak" ]; do bak="${bak%.bak}.$RANDOM.bak"; done
        cp -a "$f" "$bak"
        info "backup: $bak"
    fi
    install -d -m 700 "$WG_DIR"
    if ! awk -F'\t' -v f="$f" '$1 == f { found = 1 } END { exit !found }' "$BACKUP_MAP" 2>/dev/null; then
        printf '%s\t%s\n' "$f" "$bak" >>"$BACKUP_MAP"
        chmod 600 "$BACKUP_MAP"
    fi
}

# Write stdin to <path> with <mode> if it differs: back up first, replace
# atomically. Sets WROTE=1 on change. Dry-run prints a redacted diff instead.
write_file() {
    local f="$1" mode="$2" tmp
    tmp=$(mktemp)
    cat >"$tmp"
    WROTE=0
    if [ -f "$f" ] && cmp -s "$tmp" "$f"; then
        info "unchanged: $f"
        return 0
    fi
    WROTE=1
    if [ "$DRY_RUN" -eq 1 ]; then
        info "[dry-run] would write $f (mode $mode):"
        diff -u --label "$f" --label "$f (new)" \
            <(if [ -f "$f" ]; then redact <"$f"; fi) <(redact <"$tmp") | sed 's/^/      /' || true
        return 0
    fi
    backup_file "$f"
    if [ ! -d "$(dirname "$f")" ]; then install -d -m 755 "$(dirname "$f")"; fi
    install -m "$mode" "$tmp" "$f.wg-tools.new"
    mv -f "$f.wg-tools.new" "$f"
    info "wrote: $f"
}

# Print templates/<file> with each {{KEY}} replaced: render_template <file> KEY=value...
# Pure bash (no sed), so values may contain any character and secrets never
# reach a command line. An unfilled {{KEY}} is an error.
render_template() {
    local name="$1" tmpl="$TEMPLATE_DIR/$1" out kv
    shift
    [ -f "$tmpl" ] || die "missing template: $tmpl"
    out=$(<"$tmpl")
    for kv in "$@"; do
        out=${out//"{{${kv%%=*}}}"/"${kv#*=}"}
    done
    if [[ $out =~ \{\{[A-Z_]+\}\} ]]; then die "template $name: no value for ${BASH_REMATCH[0]}"; fi
    printf '%s\n' "$out"
}

# Refuse to overwrite a config these tools didn't create, unless --force.
ensure_managed() {
    if [ -f "$1" ] && ! grep -qF "$MARKER" "$1" && [ "$FORCE" -eq 0 ]; then
        die "$1 exists and was not created by wireguard-tools; move it aside or use --force"
    fi
}

# --- Network facts ---

default_route() { ip -4 route show default | awk 'NR == 1'; }

# "via X dev Y" from a route line, ignoring volatile fields like metric/proto.
route_key() {
    awk '{ for (i = 1; i < NF; i++) { if ($i == "via") v = $(i + 1); if ($i == "dev") d = $(i + 1) } }
         END { print "via " v " dev " d }' <<<"$1"
}
route_dev() { route_key "$1" | awk '{ print $4 }'; }

has_ipv6() { [ -n "$(ip -6 route show default 2>/dev/null)" ]; }
kernel_ipv6() { [ -f /proc/net/if_inet6 ] && [ "$(sysctl -n net.ipv6.conf.all.disable_ipv6 2>/dev/null)" = 0 ]; }
if_exists() { ip link show dev "$1" >/dev/null 2>&1; }

# Fetch one field (warp=, ip=) from Cloudflare's trace; extra args go to curl.
trace_field() {
    local field="$1" out="" try
    shift
    for try in 1 2 3; do
        out=$(curl -fsS --max-time 10 "$@" "$TRACE_URL" 2>/dev/null) && break
        [ "$try" -lt 3 ] && sleep 2
    done
    sed -n "s/^$field=//p" <<<"$out"
}

# Record the pre-install default route, public interface and ip_forward once.
record_baseline() {
    local route
    route=$(default_route)
    [ -n "$route" ] || die "no IPv4 default route; refusing to continue"
    [ "$(route_dev "$route")" != "$WARP_IF" ] || die "default route already points at $WARP_IF; fix routing first"
    if [ -z "$(state_get DEFAULT_ROUTE)" ]; then
        state_set DEFAULT_ROUTE "$route"
        state_set PUB_IF "$(route_dev "$route")"
        state_set ORIG_IP_FORWARD "$(sysctl -n net.ipv4.ip_forward)"
    fi
    # Recorded separately: servers set up before IPv6 support lack it.
    if [ -z "$(state_get ORIG_IP6_FORWARD)" ] && kernel_ipv6; then
        state_set ORIG_IP6_FORWARD "$(sysctl -n net.ipv6.conf.all.forwarding)"
    fi
    info "default route: $route (public interface $(route_dev "$route"))"
}

# Public interface: recorded at install, else the current default route's.
pub_if() {
    local p
    p=$(state_get PUB_IF)
    echo "${p:-$(route_dev "$(default_route)")}"
}

# Interface client traffic leaves through.
out_if() {
    if [ "$EXIT" = warp ]; then echo "$WARP_IF"; else pub_if; fi
}

# Abort (and stop warp) if the default route moved: SSH depends on it.
guard_default_route() {
    [ "$DRY_RUN" -eq 0 ] || return 0
    local rec
    rec=$(state_get DEFAULT_ROUTE)
    if [ "$(route_key "$(default_route)")" != "$(route_key "$rec")" ]; then
        warn "default route changed (was: $rec, now: $(default_route))"
        if [ "$EXIT" = warp ]; then systemctl stop "wg-quick@$WARP_IF" || true; fi
        die "default route changed; investigate before re-running"
    fi
}

# Client endpoint: --endpoint / ENDPOINT, saved value, or detected public IPv4.
# Saved only when <save> is 1 (wg-setup.sh direct/warp).
resolve_endpoint() {
    local save="$1" ep="$ENDPOINT"
    [ -n "$ep" ] || ep=$(state_get ENDPOINT)
    if [ -z "$ep" ]; then
        ep=$(trace_field ip -4)
        [ -n "$ep" ] || die "could not detect the public IP; pass --endpoint <ip-or-host>"
    fi
    if [[ $ep == *:* && $ep != \[* ]]; then ep="[$ep]"; fi
    ENDPOINT="$ep"
    if [ "$save" -eq 1 ] && [ "$(state_get ENDPOINT)" != "$ep" ]; then state_set ENDPOINT "$ep"; fi
    info "client endpoint: $ENDPOINT:$WG_PORT"
}

# --- Routing / NAT rules (single source for PostUp, PostDown, rollback, test) ---

# Policy rule for "ip" (IPv4) or "ip -6" with that family's subnet.
rule_spec() {
    local net="$WG_SUBNET"
    if [ "$1" = "ip -6" ]; then net="$WG_SUBNET6"; fi
    echo "from $net lookup $RT_TABLE priority $RULE_PRIO"
}

# "ip", plus "ip -6" when IPv6 is on.
ip_families() {
    echo "ip"
    if [ "$IPV6" -eq 1 ]; then echo "ip -6"; fi
}

# RT_TABLE's default route(s) out through warp (in wg0's PostUp and warp.conf's).
warp_route() {
    local f
    while IFS= read -r f; do echo "$f route replace default dev $WARP_IF table $RT_TABLE"; done < <(ip_families)
}

# Firewall rules as "command|table|chain|rule". Inserted (-I) so ufw/Docker
# DROP policies can't shadow them.
ipt_rules() {
    local out cmd net
    out=$(out_if)
    for cmd in iptables ip6tables; do
        net="$WG_SUBNET"
        if [ "$cmd" = ip6tables ]; then
            [ "$IPV6" -eq 1 ] || continue
            net="$WG_SUBNET6"
        fi
        printf '%s\n' \
            "$cmd|nat|POSTROUTING|-s $net -o $out -j MASQUERADE" \
            "$cmd|filter|FORWARD|-i $WG_IF -o $out -j ACCEPT" \
            "$cmd|filter|FORWARD|-i $out -o $WG_IF -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT" \
            "$cmd|mangle|FORWARD|-o $out -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu"
    done
    echo "iptables|filter|INPUT|-p udp --dport $WG_PORT -j ACCEPT"
}

# Shell lines for wg0's PostUp (up) or PostDown (down). Each is idempotent;
# down is the exact inverse of up, in reverse order. Policy routing is warp-only.
hook_cmds() {
    local x t c r f net
    if [ "$1" = up ]; then
        if [ "$EXIT" = warp ]; then
            while IFS= read -r f; do
                net="$WG_SUBNET"
                if [ "$f" = "ip -6" ]; then net="$WG_SUBNET6"; fi
                echo "while $f rule del $(rule_spec "$f") 2>/dev/null; do :; done; $f rule add $(rule_spec "$f")"
                echo "$f route replace $net dev $WG_IF table $RT_TABLE"
                echo "$f route replace default dev $WARP_IF table $RT_TABLE"
                echo "$f route replace blackhole default metric $BLACKHOLE_METRIC table $RT_TABLE"
            done < <(ip_families)
        fi
        while IFS='|' read -r x t c r; do
            echo "$x -t $t -C $c $r 2>/dev/null || $x -t $t -I $c $r"
        done < <(ipt_rules)
    else
        while IFS='|' read -r x t c r; do
            echo "while $x -t $t -C $c $r 2>/dev/null; do $x -t $t -D $c $r || break; done"
        done < <(ipt_rules | tac)
        if [ "$EXIT" = warp ]; then
            while IFS= read -r f; do
                net="$WG_SUBNET"
                if [ "$f" = "ip -6" ]; then net="$WG_SUBNET6"; fi
                echo "$f route del blackhole default metric $BLACKHOLE_METRIC table $RT_TABLE 2>/dev/null || true"
                echo "$f route del default dev $WARP_IF table $RT_TABLE 2>/dev/null || true"
                echo "$f route del $net dev $WG_IF table $RT_TABLE 2>/dev/null || true"
                echo "while $f rule del $(rule_spec "$f") 2>/dev/null; do :; done"
            done < <(ip_families | tac)
        fi
    fi
}

# Run the hook lines directly (rollback and error cleanup).
run_hooks() {
    local line
    while IFS= read -r line; do run bash -c "$line"; done < <(hook_cmds "$1")
}

# --- Clients registry: "name ip public-key allowed-ips" per line ---
# No private or preshared keys here; allowed-ips is comma-separated, no spaces.

# A client's config file: <server>.<client>.conf, so downloads from different
# servers don't collide.
client_conf() { echo "$CLIENT_DIR/$SERVER_NAME.$1.conf"; }

db_entries() {
    [ -f "$CLIENT_DB" ] || return 0
    grep -vE '^[[:space:]]*(#|$)' "$CLIENT_DB" || true
}
db_get() { db_entries | awk -v n="$1" '$1 == n { print $2, $3, $4; exit }'; }

# Rewrite clients.db with <name> removed, optionally adding "<name> <entry>".
db_put() {
    local name="$1" entry="${2:-}"
    write_file "$CLIENT_DB" 600 < <(
        echo "# wireguard-tools clients: name ip public-key allowed-ips (no private keys)"
        db_entries | awk -v n="$name" '$1 != n'
        if [ -n "$entry" ]; then echo "$name $entry"; fi
    )
}

# --- wg0.conf ---

# Render wg0.conf: interface + hook rules + one [Peer] per clients.db entry.
render_wg_conf() {
    local line name ip pub _ psk
    cat <<EOF
$MARKER ($EXIT mode); peers are rebuilt from $CLIENT_DB (use wg-client.sh)
[Interface]
Address = $SERVER_IP/$PFX$(if [ "$IPV6" -eq 1 ]; then echo ", $P6::1/64"; fi)
ListenPort = $WG_PORT
MTU = $WG_MTU
PostUp = wg set %i private-key $SERVER_KEY
EOF
    while IFS= read -r line; do echo "PostUp = $line"; done < <(hook_cmds up)
    while IFS= read -r line; do echo "PostDown = $line"; done < <(hook_cmds down)
    while read -r name ip pub _; do
        printf '\n[Peer]\n# %s\nPublicKey = %s\n' "$name" "$pub"
        psk="$PSK_DIR/$name.psk"
        if [ -s "$psk" ]; then printf 'PresharedKey = %s\n' "$(cat "$psk")"; fi
        printf 'AllowedIPs = %s\n' "$(client_addrs "$ip" | sed 's/,/, /')"
    done < <(db_entries)
}

write_wg_conf() {
    ensure_managed "$WG_CONF"
    write_file "$WG_CONF" 600 < <(render_wg_conf)
}

# Enable an interface's wg-quick unit for boot and make it match its config.
start_unit() {
    local ifc="$1" changed="$2" unit="wg-quick@$1" enabled=0
    if ! if_exists "$ifc"; then
        step "Bring up $ifc ($unit, enabled at boot)" "systemctl disable --now $unit"
        confirm "Bring up $ifc now?"
        run systemctl enable --now "$unit"
        enabled=1
    elif [ "$changed" -eq 1 ]; then
        step "Restart $unit to apply its new config" "restore the .bak in $WG_DIR, then systemctl restart $unit"
        confirm "Restart $ifc now?"
        if systemctl is-active -q "$unit"; then
            run systemctl restart "$unit"
        else
            run wg-quick down "$ifc"
            run systemctl enable --now "$unit"
            enabled=1
        fi
    else
        info "$ifc is up and unchanged"
        systemctl is-active -q "$unit" || warn "$ifc is up but not via $unit (started by hand?)"
    fi
    if [ "$enabled" -eq 0 ] && ! systemctl is-enabled -q "$unit" 2>/dev/null; then run systemctl enable "$unit"; fi
}

# Human-readable age of a handshake timestamp.
ago() {
    local ts="$1" d
    if [ "${ts:-0}" -eq 0 ]; then echo never; return 0; fi
    d=$(( $(date +%s) - ts ))
    if [ "$d" -lt 120 ]; then echo "${d}s ago"
    elif [ "$d" -lt 7200 ]; then echo "$(( d / 60 ))m ago"
    else echo "$(( d / 3600 ))h ago"; fi
}
