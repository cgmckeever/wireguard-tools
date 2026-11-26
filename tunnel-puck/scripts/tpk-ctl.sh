#!/bin/sh
# tpk-ctl.sh: set up and run tunnel-puck from your computer (macOS or Linux). A
# puck's settings are ~/wg-tunnels/pucks/<puck>/puck.conf ($WG_TUNNELS, as
# wgt-ctl), plus one pucks/<puck>/gateways/<gateway>.conf for each gateway it
# serves (a WireGuard gateway: the server your clients connect to, which sends
# them on to the puck; the file holds the link, routing and jumps there). It runs puck.sh on the puck and gateway.sh on the gateways over ssh.
# <puck> can be left out while there's only one (else it asks; TPK_PUCK=<puck> is
# the default). <puck>.<gateway> names one gateway: commands about one gateway take
# the puck's only one without it, or ask (TPK_GATEWAY=<gateway> is the default); the
# commands about every gateway (config, status, test, all, disable, enable) then do
# just that one.
#
#   tpk-ctl.sh new [<puck>] [--wifi]              a new puck's settings file (asks the name; --wifi: its Wi-Fi list too)
#   tpk-ctl.sh [<puck>] config                    show the settings, the puck's and each gateway's, and check them
#   tpk-ctl.sh [<puck>] ssh-config                print the ~/.ssh/config entry for SSH_CLOUDFLARE
#   tpk-ctl.sh ssh-config install                 add every puck's entry to your ssh config (its own file, one Include)
#   tpk-ctl.sh [<puck>] init                      1.2c, on the puck: packages, updates, watchdog (and cloudflared, with SSH_CLOUDFLARE)
#   tpk-ctl.sh [<puck>] gateway add <gateway>     2.1: its settings file (gateways/<gateway>.conf), then link, exit, test
#   tpk-ctl.sh [<puck>] gateway [list]            the gateways the puck serves: routing, link, interfaces, jumps
#   tpk-ctl.sh [<puck>] gateway remove <gateway>  stop serving one: its configs and jumps (wgt), its link, its settings file
#   tpk-ctl.sh [<puck>[.<gateway>]] link          the link to the gateway (puck and gateway), keys exchanged for you
#   tpk-ctl.sh [<puck>[.<gateway>]] exit          by TPK_ROUTING: tpk's DNS resolver, or the wgt jump(s)
#   tpk-ctl.sh [<puck>[.<gateway>]] client <device> [--egress] [--lan]   2.2, TPK_ROUTING=wgt: a config for <device> through
#                                                 the puck: the internet (no flags, or --egress), the internet and the
#                                                 friend's LAN (--egress --lan: JUMP_LAN), or the LAN only (--lan: JUMP_LANONLY)
#   tpk-ctl.sh [<puck>[.<gateway>]] mode [--egress [--lan]] [<interface>]   2.3: the internet only, or the friend's LAN too
#                                                 (default: wgpk<n> or wgrtun<n>); no flags: show it
#   tpk-ctl.sh [<puck>[.<gateway>]] disable [<device>]   the puck's clients (or one device's) on each gateway, or that one, off;
#                                                 links, keys and configs stay
#   tpk-ctl.sh [<puck>[.<gateway>]] enable [<device>]   let back in what disable turned off
#   tpk-ctl.sh [<puck>[.<gateway>]] status [--watch|--alert [s]]   at a glance: the puck, and per gateway its link, rtun,
#                                                 jump and each config's (tpk: each client's) last handshake; --watch: redrawn
#                                                 every 5s; --alert: and a bell when one connects
#   tpk-ctl.sh [<puck>] wifi [list]               the puck's saved Wi-Fi networks, and its uplink now
#   tpk-ctl.sh [<puck>] wifi add <name>           save a Wi-Fi network on the puck (asks for the password)
#   tpk-ctl.sh [<puck>] wifi remove <name>|all    remove saved Wi-Fi (all: ethernet only)
#   tpk-ctl.sh [<puck>] wifi sync                 the puck's Wi-Fi becomes pucks/<puck>/wifi.conf, in its order
#   tpk-ctl.sh [<puck>[.<gateway>]] test          PASS/FAIL: the puck, then each gateway (wgt: and a client's way out)
#   tpk-ctl.sh [<puck>] ssh [<command>]           a shell (or a command) on the puck, the first way that answers
#   tpk-ctl.sh [<puck>] reboot                    restart the puck, and wait until it answers again
#   tpk-ctl.sh [<puck>] shutdown                  power the puck off, safe to unplug (asks first)
#   tpk-ctl.sh [<puck>[.<gateway>]] all           each gateway (or that one): link, exit; then wifi sync (with a wifi.conf), test
#   tpk-ctl.sh [<puck>] undo [all]                take it off every gateway and the puck (all: cloudflared too); the settings stay
#
# The puck is reached the first way that answers: SSH_LAN (at home), then through
# each gateway over its link, then SSH_CLOUDFLARE (the emergency door, for when the
# links are down). Anything that changes a gateway asks first; TPK_YES=1 answers yes
# (only once the person has agreed to these changes on that gateway). TPK_DRY_RUN=1
# shows what would run and changes nothing.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
EXAMPLES=$(cd "$HERE/../configs" && pwd)    # the templates; settings live in PUCKS
REPO=$(cd "$HERE/../.." && pwd)
# ~/wg-tunnels ($WG_TUNNELS), by wgt-ctl's own rule (wgt/ctl/home.py).
STORE=$(PYTHONPATH="$REPO" python3 -c 'import sys
from wgt.ctl import home
from wgt.errors import WgtError
try: print(home.root())
except WgtError as e: sys.exit(f"tpk-ctl.sh: {e}")') || exit 1
PUCKS="$STORE/pucks"
WGT_CTL="$REPO/bin/wgt-ctl"
COMMANDS="new config ssh-config init gateway link exit client mode disable enable status wifi test ssh reboot shutdown all undo"
# Cloudflare's trace (ip=... lines), as wgt's jump test uses (wgt/const.py TRACE_URL).
TRACE_URL="https://www.cloudflare.com/cdn-cgi/trace"
# The remote prefix that runs a command as root (sudo unless already root).
AS_ROOT='if [ "$(id -u)" = 0 ]; then S=; else S=sudo; fi; $S'
# The settings in a pucks/<puck>/gateways/<gateway>.conf.
GATEWAY_VARS="TPK_ROUTING PUCK_PORT TPK_LINK GATEWAY_SSH GATEWAY_HOST CLIENT_IPS CLIENT_IP6S WG_IF WG_SUBNET
          WGT_SERVER JUMP_CODE JUMP_LABEL JUMP_LAN JUMP_LAN_CODE JUMP_LAN_LABEL JUMP_LANONLY JUMP_LANONLY_CODE
          JUMP_LANONLY_LABEL TPK_DISABLED TPK_DISABLED_CONFIGS"
# link_ids, name_ok, gateway_name_ok: the link's names and numbers, as puck.sh and gateway.sh have them.
# shellcheck source=ids.sh
. "$HERE/ids.sh"


# --- Small helpers -------------------------------------------------------------

log() { printf '==> %s\n' "$*" >&2; }
die() { printf 'tpk-ctl.sh: %s\n' "$*" >&2; exit 1; }
usage() {
    awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "$0"
    printf '\nconfigured pucks: %s\n' "$(pucks | tr '\n' ' ' | grep . || echo 'none yet (scripts/tpk-ctl.sh new)')"
}

q() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }    # the value quoted for sh
placeholder() { case "$1" in ''|*example.com*|*'<'*) return 0 ;; esac; return 1; }
is_command() { case " $COMMANDS " in *" $1 "*) return 0 ;; esac; return 1; }
have_tty() { (: < /dev/tty) 2>/dev/null; }
puck_name_ok() { ( PUCK_NAME="$1" TPK_LINK=""; link_ids ) || exit 1; }    # in a subshell: sets nothing here
words() { printf '%s\n' "$1" | tr '\n' ' ' | sed 's/ *$//'; }    # one per line -> one line

# ask_yes <question>: true on y/yes, asked on the terminal.
ask_yes() {
    printf '%s [y/N] ' "$1" >&2
    read -r yes < /dev/tty || yes=""
    case "$yes" in y|Y|yes) return 0 ;; esac
    return 1
}

# require <SETTING>...: each is set, and not still the example's value.
require() {
    for s in "$@"; do
        eval "val=\${$s}"
        case "$s" in SSH_*|CF_*) f="$PUCK_CONF" ;; *) f="$CONF" ;; esac
        ! placeholder "$val" || die "$s in $f is empty or still the example"
    done
}

# save <SETTING> <value>: set SETTING="value" in the settings file being used (the
# gateway's, for its settings), replacing its line or adding one, and say so.
save() {
    tmp=$(mktemp)
    awk -v k="$1" -v line="$1=\"$2\"" 'index($0, k "=") == 1 && !done {print line; done = 1; next} {print}
        END {if (!done) print line}' "$CONF" > "$tmp"
    cat "$tmp" > "$CONF"; rm -f "$tmp"
    log "saved $1=\"$2\" in $(shown "$CONF")"
}

# choose <what> <names, one per line> <default> <hint>: PICK. One name: that one.
# Several: a numbered list, <default> for Enter; with no terminal, <default>
# itself, else stop with <hint>.
choose() {
    what="$1" names="$2" default="$3" hint="$4"
    count=$(printf '%s\n' "$names" | grep -c . || true)
    if [ -n "$default" ] && ! printf '%s\n' "$names" | grep -qx -- "$default"; then
        die "the default $what '$default' isn't one of: $(words "$names")"
    fi
    if [ "$count" = 1 ]; then PICK="$names"; return 0; fi
    if ! have_tty; then
        [ -n "$default" ] || die "$hint (one of: $(words "$names"))"
        PICK="$default"; return 0
    fi
    printf 'Which %s?\n' "$what" >&2
    i=0
    for n in $names; do
        i=$((i + 1))
        if [ "$n" = "$default" ]; then printf '  %d) %s  (default)\n' "$i" "$n" >&2; else printf '  %d) %s\n' "$i" "$n" >&2; fi
    done
    printf '[number or name%s]: ' "${default:+, Enter for $default}" >&2
    read -r answer < /dev/tty || answer=""
    [ -n "$answer" ] || answer="$default"
    case "$answer" in
        '') die "no $what chosen" ;;
        *[!0-9]*) PICK="$answer" ;;
        *) PICK=$(printf '%s\n' "$names" | sed -n "${answer}p") ;;
    esac
    printf '%s\n' "$names" | grep -qx -- "$PICK" || die "no $what '$answer' (one of: $(words "$names"))"
}


# --- Which puck, which gateway -----------------------------------------------------

# Each puck's settings: pucks/<puck>/puck.conf, its Wi-Fi list wifi.conf, and one
# gateways/<gateway>.conf per gateway it serves. Folders 700, files 600.
puck_file() { echo "$PUCKS/$1/puck.conf"; }
wifi_file() { echo "$PUCKS/$1/wifi.conf"; }
gateway_file() { echo "$PUCKS/$1/gateways/$2.conf"; }
# shown <path>: under ~ when it is, for messages.
shown() { case "$1" in "$HOME"/*) echo "~${1#"$HOME"}" ;; *) echo "$1" ;; esac; }
# private_dir <dir>: made, mode 700 (with its parents up to the store).
private_dir() { (umask 077; mkdir -p "$1"); chmod 700 "$1"; }

# pucks: the configured pucks, one name per line: those with a pucks/<puck>/puck.conf.
pucks() {
    for f in "$PUCKS"/*/puck.conf; do
        [ -e "$f" ] || continue
        f=${f%/puck.conf}; echo "${f##*/}"
    done
    return 0
}

# gateways_of <puck>: the gateways it serves, one per line: pucks/<puck>/gateways/<gateway>.conf.
gateways_of() {
    for f in "$PUCKS/$1"/gateways/*.conf; do
        [ -e "$f" ] || continue
        basename "$f" .conf
    done
    return 0
}
gateways() { gateways_of "$PUCK_NAME"; }

# ask_puck <command>: PICK, when the command line names no puck.
ask_puck() {
    names=$(pucks)
    [ -n "$names" ] || die "no settings yet: scripts/tpk-ctl.sh new, then fill it in"
    choose puck "$names" "${TPK_PUCK:-}" "which puck? tpk-ctl.sh <puck> $1, or TPK_PUCK=<puck>"
}

# one_gateway <command>: load the gateway a command works on: the one named (<puck>.<gateway>),
# else the puck's only one, else ask.
one_gateway() {
    if [ -n "$GATEWAY_PICK" ]; then load_gateway "$GATEWAY_PICK"; return 0; fi
    names=$(gateways)
    [ -n "$names" ] || die "$PUCK_NAME serves no gateway yet: tpk-ctl.sh $PUCK_NAME gateway add <gateway>"
    choose gateway "$names" "${TPK_GATEWAY:-}" "which gateway? tpk-ctl.sh $PUCK_NAME.<gateway> $1, or TPK_GATEWAY=<gateway>"
    load_gateway "$PICK"
}

# the_gateways: the gateways the commands about every gateway go through: the one named, else all.
the_gateways() { if [ -n "$GATEWAY_PICK" ]; then echo "$GATEWAY_PICK"; else gateways; fi; }

# cmd_new [<name>] [--wifi]: a new puck's settings file from the template (and, with
# --wifi or a yes, its Wi-Fi list), asking for the name when it isn't given.
cmd_new() {
    name="" wifi=""
    for a in "$@"; do case "$a" in --wifi) wifi=1 ;; *) name="$a" ;; esac; done
    if [ -z "$name" ]; then
        have_tty || die "tpk-ctl.sh new <name> [--wifi]"
        printf 'Name for the new puck (up to 10 of a-z, 0-9 and -, starting with a letter): ' >&2
        read -r name < /dev/tty || name=""
    fi
    puck_name_ok "$name"
    ! is_command "$name" || die "'$name' is a command word: pick another name"
    f=$(puck_file "$name") w=$(wifi_file "$name")
    [ ! -e "$f" ] || die "$(shown "$f") already exists"
    private_dir "$PUCKS"; private_dir "$PUCKS/$name"
    (umask 077; cp "$EXAMPLES/puck.conf.example" "$f")
    log "made $(shown "$f"): fill it in, then scripts/tpk-ctl.sh $name config"
    if [ -z "$wifi" ] && have_tty && [ ! -e "$w" ]; then
        ! ask_yes "A Wi-Fi list for it too ($(shown "$w"))?" || wifi=1
    fi
    if [ -n "$wifi" ]; then
        [ ! -e "$w" ] || die "$(shown "$w") already exists"
        (umask 077; cp "$EXAMPLES/puck.wifi.conf.example" "$w")
        log "made $(shown "$w"): one name::password per line, most preferred first"
    fi
    log "then: tpk-ctl.sh $name init (1.2c), and tpk-ctl.sh $name gateway add <gateway> for each gateway it serves (2.1)"
}


# --- Settings ------------------------------------------------------------------

# load_puck: read pucks/<PUCK_NAME>/puck.conf, the puck's own settings.
load_puck() {
    puck_name_ok "$PUCK_NAME"
    PUCK_CONF=$(puck_file "$PUCK_NAME")
    [ -r "$PUCK_CONF" ] || die "no puck '$PUCK_NAME': $(shown "$PUCK_CONF") doesn't exist (configured: $(words "$(pucks)"))"
    log "puck: $PUCK_NAME ($(shown "$PUCK_CONF"))"
    SSH_LAN="" SSH_CLOUDFLARE="" CF_TOKEN_FILE=""
    # shellcheck disable=SC1090
    . "$PUCK_CONF"
    PUCK_USER=$(user_of "$SSH_LAN")
    reset_gateway
    # Sent to the scripts when a step sets them; empty otherwise.
    CF_TOKEN="" WIFI_SSID="" WIFI_PSK="" WIFI_LIST=""
}

# reset_gateway: no gateway loaded: its settings empty, saves go to the puck's file.
reset_gateway() {
    for s in $GATEWAY_VARS; do eval "$s=''"; done
    GATEWAY_NAME="" CONF="$PUCK_CONF" CONFIRMED="" TPK_LINK_AVOID="" PUCK_PUBKEY="" GATEWAY_PUBKEY=""
    link_ids
}

# load_gateway <gateway>: read pucks/<puck>/gateways/<gateway>.conf and fill in what follows from it.
# QUIET=1: no log line, and no questions (stops instead).
load_gateway() {
    gateway_name_ok "$1"
    reset_gateway
    GATEWAY_NAME="$1" CONF=$(gateway_file "$PUCK_NAME" "$1")
    [ -r "$CONF" ] || die "$PUCK_NAME serves no gateway '$1': $(shown "$CONF") doesn't exist (its gateways: $(words "$(gateways)"); add one: tpk-ctl.sh $PUCK_NAME gateway add $1)"
    [ -n "${QUIET:-}" ] || log "gateway: $1 ($(shown "$CONF"))"
    # shellcheck disable=SC1090
    . "$CONF"
    : "${TPK_ROUTING:=tpk}" "${WG_IF:=wg0}"
    JUMP_CODE=$(jump_code "$JUMP_CODE" "$PUCK_NAME")
    : "${JUMP_LABEL:=$PUCK_NAME}"
    JUMP_LAN_CODE=$(lan_code "$JUMP_LAN_CODE" "$JUMP_LAN" "$JUMP_CODE" a)
    [ -z "$JUMP_LAN_CODE" ] || : "${JUMP_LAN_LABEL:=$JUMP_LABEL (egress + LAN)}"
    JUMP_LANONLY_CODE=$(lan_code "$JUMP_LANONLY_CODE" "$JUMP_LANONLY" "$JUMP_CODE" l)
    [ -z "$JUMP_LANONLY_CODE" ] || : "${JUMP_LANONLY_LABEL:=$JUMP_LABEL (LAN only)}"
    link_ids
    case "$TPK_ROUTING" in
        wgt) wgt_gateway_settings ;;
        tpk) [ -z "$WGT_SERVER" ] || die "WGT_SERVER is for TPK_ROUTING=wgt; with tpk, empty it and set GATEWAY_SSH and GATEWAY_HOST" ;;
        *) die "TPK_ROUTING must be tpk or wgt (in $CONF)" ;;
    esac
}

# Jump codes are wgt's: 2-6 of a-z, 0-9 and -. A puck's name can be longer (up to
# 10), so the defaults are shortened to fit; config shows them, and checks them.
# shorten <text> <n>: its first n characters, without a trailing -.
shorten() {
    t=$(printf '%s' "$1" | cut -c "1-$2")
    while case "$t" in *-) true ;; *) false ;; esac; do t="${t%-}"; done
    printf '%s\n' "$t"
}
# jump_code <JUMP_CODE> <puck name>: the jump code; empty, the puck's name (shortened to 6).
jump_code() { if [ -n "$1" ]; then echo "$1"; else shorten "$2" 6; fi; }
# lan_code <its code> <on> <jump code> <letter>: a LAN jump's code, if there's one:
# the code set, else when on (yes) the jump code with -<letter>. The LAN jump
# (JUMP_LAN, egress + LAN) is -a, for all (mom: mom-a); the LAN-only jump
# (JUMP_LANONLY) -l (mom: mom-l).
lan_code() {
    if [ -n "$1" ]; then echo "$1"
    elif [ "$2" = yes ]; then echo "$(shorten "$3" 4)-$4"; fi
}

# jump_codes: the loaded gateway's jump codes, on one line: the jump's, then the LAN
# jump's and the LAN-only jump's, when they're on.
# shellcheck disable=SC2086
jump_codes() { echo $JUMP_CODE $JUMP_LAN_CODE $JUMP_LANONLY_CODE; }

# rtun_ifs: the puck's rtuns for the loaded gateway's jumps, on one line.
rtun_ifs() {
    ifs="$RTUN_IF"
    [ -z "$JUMP_LAN_CODE" ] || ifs="$ifs $RTUN_LAN_IF"
    [ -z "$JUMP_LANONLY_CODE" ] || ifs="$ifs $RTUN_LO_IF"
    echo "$ifs"
}

# wgt_gateway_settings: TPK_ROUTING=wgt drives the gateway through wgt-ctl: the gateway is
# WGT_SERVER (empty: the gateway's own name, if wgt-ctl lists it, else asked for), and
# GATEWAY_SSH (and GATEWAY_HOST, unless set) come from wgt-ctl's list, read by its own code.
wgt_gateway_settings() {
    [ -x "$WGT_CTL" ] || die "TPK_ROUTING=wgt needs wgt-ctl at $WGT_CTL (tunnel-puck inside a wireguard-tools checkout)"
    if [ -z "$WGT_SERVER" ]; then
        if wgt_servers 2>/dev/null | grep -qxF -- "$GATEWAY_NAME"; then WGT_SERVER="$GATEWAY_NAME"
        elif [ -n "${QUIET:-}" ]; then die "WGT_SERVER is empty in $CONF"
        else choose_wgt_server; fi
    fi
    GATEWAY_SSH=$(PYTHONPATH="$REPO" python3 -c 'import sys; from wgt.ctl import remote; print(remote.find(sys.argv[1]).target)' \
        "$WGT_SERVER" 2>/dev/null) || die "WGT_SERVER '$WGT_SERVER' isn't in wgt-ctl's list (see: bin/wgt-ctl list)"
    ! placeholder "$GATEWAY_HOST" || GATEWAY_HOST="${GATEWAY_SSH#*@}"
}

wgt_servers() {    # wgt-ctl's servers, one name per line
    PYTHONPATH="$REPO" python3 -c 'from wgt.ctl import remote
for s in remote.servers(): print(s.name)'
}

# choose_wgt_server: WGT_SERVER, when it's empty: a server from wgt-ctl's list, or a
# new one added with wgt-ctl init (the default when there are none). Saved in the
# gateway's settings file. Needs a terminal.
choose_wgt_server() {
    have_tty || die "TPK_ROUTING=wgt goes through wgt-ctl: set WGT_SERVER in $CONF to the gateway's name in bin/wgt-ctl list (not there yet? bin/wgt-ctl init adds it)"
    before=$(wgt_servers) || die "can't read wgt-ctl's server list (bin/wgt-ctl list says why)"
    printf 'TPK_ROUTING=wgt: which wgt server is %s (for %s)?\n' "$GATEWAY_NAME" "$PUCK_NAME" >&2
    i=0
    for n in $before; do i=$((i + 1)); printf '  %d) %s\n' "$i" "$n" >&2; done
    printf '  a) add a new one: bin/wgt-ctl init sets up wgt on it (or adopts one already running it)\n' >&2
    if [ -z "$before" ]; then printf '[a]: ' >&2; else printf '[number or a]: ' >&2; fi
    read -r answer < /dev/tty || answer=""
    [ -n "$answer" ] || [ -n "$before" ] || answer=a
    case "$answer" in
        a|A)
            [ -z "$DRY" ] || die "a dry run doesn't add a server: run it without TPK_DRY_RUN, or set WGT_SERVER"
            ask_yes "wgt-ctl init connects to that server and sets it up (or adopts it). Continue?" \
                || die "stopped; nothing was changed"
            "$WGT_CTL" init < /dev/tty || die "wgt-ctl init didn't finish: nothing was saved"
            WGT_SERVER=$(wgt_servers | grep -vxF -e "$before" -e '' | head -n 1 || true)
            [ -n "$WGT_SERVER" ] || die "wgt-ctl init added no server: set WGT_SERVER by hand"
            ;;
        ''|*[!0-9]*) die "no server chosen" ;;
        *)  WGT_SERVER=$(printf '%s\n' "$before" | sed -n "${answer}p")
            [ -n "$WGT_SERVER" ] || die "no server $answer" ;;
    esac
    if [ -n "$DRY" ]; then log "[dry-run] would save WGT_SERVER=\"$WGT_SERVER\""; else save WGT_SERVER "$WGT_SERVER"; fi
}

# The settings puck.sh and gateway.sh read, as sh assignments (sent ahead of ids.sh and the script).
vars() {
    for s in PUCK_NAME TPK_LINK TPK_LINK_AVOID GATEWAY_HOST PUCK_PORT TPK_ROUTING TPK_DISABLED WG_IF JUMP_CODE JUMP_LAN_CODE JUMP_LANONLY_CODE CLIENT_IPS \
             CLIENT_IP6S WG_SUBNET SSH_CLOUDFLARE PUCK_PUBKEY GATEWAY_PUBKEY CF_TOKEN WIFI_SSID WIFI_PSK WIFI_LIST; do
        eval "val=\${$s}"
        printf '%s=%s\n' "$s" "$(q "$val")"
    done
    [ -z "${TPK_RENEW:-}" ] || echo "TPK_RENEW=$(q "$TPK_RENEW")"
}


# --- Reaching the puck and the gateway -----------------------------------------------

# PUCK (an ssh target), PUCK_ROUTE (lan, link or cloudflare), PUCK_HOP (the
# gateway, an ssh -J hop, for a link), PUCK_GATEWAY (that gateway's name) and PUCK_VIA (said
# in the log): set once per run by pick_puck.
PUCK="" PUCK_ROUTE="" PUCK_HOP="" PUCK_GATEWAY="" PUCK_VIA=""

# user_of <SSH_LAN>: the puck's user, the part before an @ (none: ssh's default).
user_of() { case "$1" in *@*) printf '%s\n' "${1%%@*}" ;; esac; }
probe() { ssh -n -o BatchMode=yes -o ConnectTimeout=8 "$@" true 2>/dev/null; }
link_target() { if [ -n "$PUCK_USER" ]; then echo "$PUCK_USER@$1"; else echo "$1"; fi; }
# Through a link, the puck's host key is kept under its name, in tunnel-puck's
# own known_hosts, not under its link address: link addresses get reused (a
# reflashed puck, a new puck on an old one's subnet). It's taken on first use,
# and link forgets it (the link itself is authenticated by WireGuard keys).
KNOWN_HOSTS="$HOME/.config/tunnel-puck/known_hosts"
link_opts() {
    mkdir -p "${KNOWN_HOSTS%/*}"
    printf '%s\n' -o "HostKeyAlias=tunnel-puck-$PUCK_NAME" -o "UserKnownHostsFile=$KNOWN_HOSTS" \
        -o StrictHostKeyChecking=accept-new -J "$1"
}
forget_link_key() { ssh-keygen -R "tunnel-puck-$PUCK_NAME" -f "$KNOWN_HOSTS" >/dev/null 2>&1 || true; }

# Through Cloudflare (the emergency door), cloudflared is the ssh proxy, given on
# the command line: no ~/.ssh/config entry is needed (ssh-config install adds one
# for your own ssh).
cloudflared_bin() {
    command -v cloudflared \
        || die "the emergency door needs cloudflared on this computer (macOS: brew install cloudflared; Linux: Cloudflare's package)"
}
cf_opts() {
    printf '%s\n' -o "ProxyCommand=$(cloudflared_bin) access ssh --hostname %h"
    [ -z "$PUCK_USER" ] || printf '%s\n' -o "User=$PUCK_USER"
}

# route_opts: ssh options for the puck, one per line, as pick_puck found it.
route_opts() {
    case "$PUCK_ROUTE" in
        link) link_opts "$PUCK_HOP" ;;
        cloudflare) cf_opts ;;
    esac
}

# puck_ssh <command> <args...>: <command> (ssh, probe ...) with the route's
# options and the puck's target, then <args>. The options split on lines only:
# cloudflared's ProxyCommand holds spaces.
puck_ssh() {
    c="$1"; shift
    opts=$(route_opts) || exit 1
    old_ifs="$IFS"; IFS='
'
    # shellcheck disable=SC2086
    set -- $opts "$PUCK" "$@"
    IFS="$old_ifs"
    "$c" "$@"
}
probe_puck() { puck_ssh probe; }

# gateway_hop <gateway>: "<the gateway's ssh target> <the puck's link address>" for a gateway the
# puck has a link to (nothing if it has none yet). Read in a subshell, so the gateway
# loaded now stays as it is.
gateway_hop() {
    ( QUIET=1; load_gateway "$1" >/dev/null 2>&1; [ -n "$PUCK_LINK" ] && printf '%s %s\n' "$GATEWAY_SSH" "$PUCK_LINK" ) || true
}

# pick_puck [<gateway>]: how to reach the puck, the first way that answers: SSH_LAN,
# each gateway's link (not <gateway>'s: the one being taken down), then Cloudflare.
pick_puck() {
    [ -z "$PUCK" ] || return 0
    skip="${1:-}"
    require SSH_LAN
    if [ -n "$DRY" ]; then PUCK="$SSH_LAN" PUCK_ROUTE=lan PUCK_VIA="SSH_LAN (dry run: not tried)"
    elif probe "$SSH_LAN"; then
        PUCK="$SSH_LAN" PUCK_ROUTE=lan PUCK_VIA="SSH_LAN"
    else
        for v in $(gateways); do
            [ "$v" != "$skip" ] || continue
            hop=$(gateway_hop "$v"); [ -n "$hop" ] || continue
            via="${hop% *}" target=$(link_target "${hop#* }")
            # shellcheck disable=SC2046
            if probe $(link_opts "$via") "$target"; then
                PUCK="$target" PUCK_ROUTE=link PUCK_HOP="$via" PUCK_GATEWAY="$v" PUCK_VIA="the link to $v, through $via"
                break
            fi
        done
        if [ -z "$PUCK" ] && [ -n "$SSH_CLOUDFLARE" ]; then
            PUCK="$SSH_CLOUDFLARE" PUCK_ROUTE=cloudflare PUCK_VIA="SSH_CLOUDFLARE: Cloudflare, the emergency door (SSH_LAN and the links didn't answer)"
        elif [ -z "$PUCK" ]; then
            others=""; [ -z "$(gateways)" ] || others=", nor did its links${skip:+ (not counting the one to $skip, which this takes down)}"
            die "can't reach $PUCK_NAME: SSH_LAN ($SSH_LAN) didn't answer$others, and there's no emergency door (SSH_CLOUDFLARE)"
        fi
    fi
    log "puck: $PUCK, via $PUCK_VIA"
}

# route_around <gateway>: before taking down <gateway>'s link, reach the puck another way
# if that's the link in use.
route_around() {
    [ "$PUCK_ROUTE" = link ] && [ "$PUCK_GATEWAY" = "$1" ] || return 0
    log "the puck is reached through $1's link, which this takes down: finding another way"
    PUCK=""; pick_puck "$1"
}

# to puck|gateway <ssh args...>: ssh to that machine (the gateway through wgt-ctl, with wgt).
to() {
    who="$1"; shift
    if [ "$who" = puck ]; then puck_ssh ssh "$@"
    elif [ -n "$WGT_SERVER" ]; then "$WGT_CTL" "$WGT_SERVER" ssh "$@"
    else ssh "$GATEWAY_SSH" "$@"; fi
}

# run puck|gateway <script> <args...>: send the settings, ids.sh and the script; run it as root.
run() {
    who="$1" script="$2"; shift 2
    if [ -n "$DRY" ]; then
        if [ "$who" = puck ]; then log "[dry-run] on $PUCK: $script $*"; else log "[dry-run] on $GATEWAY_SSH: $script $*"; fi
        return 0
    fi
    { vars; cat "$HERE/ids.sh" "$HERE/$script"; } | to "$who" "$AS_ROOT sh -s -- $*"
}

# wgt_gateway <wgt args...>: a wgt command on the gateway through wgt-ctl, which also
# delivers changed client configs to ~/wg-tunnels and keeps each device's
# fleet-wide number. stdin is passed through.
wgt_gateway() {
    if [ -n "$DRY" ]; then log "[dry-run] bin/wgt-ctl $WGT_SERVER $*"; return 0; fi
    "$WGT_CTL" "$WGT_SERVER" "$@"
}

# confirm_gateway <what>: ask once per gateway before changing it (TPK_YES=1: yes).
confirm_gateway() {
    [ -z "$DRY" ] || return 0
    [ "${TPK_YES:-}" != 1 ] || return 0
    [ -z "$CONFIRMED" ] || return 0
    ask_yes "This changes the gateway $GATEWAY_NAME ($GATEWAY_SSH: $1). Continue?" || die "stopped; nothing more was changed on $GATEWAY_NAME"
    CONFIRMED=1
}

# Install the exit's rules script on the puck (every routing uses it).
install_exit_script() {
    if [ -n "$DRY" ]; then log "[dry-run] on $PUCK: install tunnel-puck-exit"; return 0; fi
    to puck "$AS_ROOT install -m 755 /dev/stdin /usr/local/sbin/tunnel-puck-exit" < "$HERE/tunnel-puck-exit"
}


# --- Settings commands ---------------------------------------------------------

cmd_config() {
    printf 'puck: %s\n' "$PUCK_CONF"
    show_settings SSH_LAN SSH_CLOUDFLARE CF_TOKEN_FILE
    require SSH_LAN
    if [ -n "$SSH_CLOUDFLARE" ]; then
        require SSH_CLOUDFLARE CF_TOKEN_FILE
        [ -r "$CF_TOKEN_FILE" ] || log "note: CF_TOKEN_FILE $CF_TOKEN_FILE isn't readable yet (tpk-ctl.sh init needs it: step 1.2b)"
    else
        log "note: no SSH_CLOUDFLARE, so no emergency door: if the links are down after drop-off, the puck must be power-cycled"
    fi
    names=$(the_gateways)
    [ -n "$names" ] || log "note: $PUCK_NAME serves no gateway yet: tpk-ctl.sh $PUCK_NAME gateway add <gateway>"
    for v in $names; do load_gateway "$v"; gateway_config; done
    log "settings look complete"
}

# show_settings <SETTING>...: one line each, its name and value.
show_settings() {
    for s in "$@"; do
        eval "val=\${$s}"
        printf '  %-15s %s\n' "$s" "${val:-(empty)}"
    done
}

# gateway_config: the loaded gateway's settings, shown and checked.
gateway_config() {
    printf 'gateway %s: %s\n' "$GATEWAY_NAME" "$CONF"
    # shellcheck disable=SC2086
    show_settings $GATEWAY_VARS
    if [ -n "$LINK_NET" ]; then
        ifs="$PK_IF"; [ "$TPK_ROUTING" != wgt ] || ifs="$ifs $(rtun_ifs)"
        printf '  %-15s %s\n' "(on the puck)" "$ifs"
    fi
    require GATEWAY_SSH GATEWAY_HOST
    case "$PUCK_PORT" in *[!0-9]*) die "PUCK_PORT must be a number, or empty (link picks one)" ;; esac
    if [ "$TPK_ROUTING" = wgt ]; then
        for code in $(jump_codes); do    # wgt's own rule for a new jump's code
            PYTHONPATH="$REPO" python3 -c 'import sys; from wgt import store; sys.exit(not store.new_exit_code(sys.argv[1]))' "$code" \
                || die "jump code '$code' won't do: $(PYTHONPATH="$REPO" python3 -c 'from wgt import store; print(store.EXIT_CODE_RULE)'). Set JUMP_CODE (or JUMP_LAN_CODE, JUMP_LANONLY_CODE) yourself"
        done
    elif [ -z "$CLIENT_IPS" ]; then
        log "note: CLIENT_IPS is empty, so no client goes through the puck on $GATEWAY_NAME yet (README 2.2)"
    fi
    check_others
}

# server_of <routing> <WGT_SERVER> <GATEWAY_SSH>: what says two settings files are the
# same gateway: the wgt server's name, or the ssh target.
server_of() { if [ "$1" = wgt ]; then echo "wgt:$2"; else echo "ssh:$3"; fi; }

# check_others: refuse what clashes with the loaded gateway's settings. The same puck:
# not the same gateway twice, and never the same TPK_LINK (its interfaces are named
# from it). Another puck on the same gateway: not its TPK_LINK, PUCK_PORT, and by
# routing a jump code (wgt) or a client address (tpk). The gateway checks the link's
# subnet and port again when link runs.
# theirs <SETTING>: its value in the other gateway file check_others is at, pucks/<p>/gateways/<o>.conf
# ($f), read in a subshell so that file's settings don't leak into these.
theirs() {
    # shellcheck disable=SC1090
    ( for s in $GATEWAY_VARS; do eval "$s=''"; done; TPK_ROUTING=tpk
      . "$f"; [ "$TPK_ROUTING" != wgt ] || : "${WGT_SERVER:=$o}"; eval "echo \"\${$1}\"" )
}

check_others() {
    me=$(server_of "$TPK_ROUTING" "$WGT_SERVER" "$GATEWAY_SSH")
    for p in $(pucks); do
        for o in $(gateways_of "$p"); do
            f=$(gateway_file "$p" "$o")
            [ "$f" != "$CONF" ] || continue
            them=$(server_of "$(theirs TPK_ROUTING)" "$(theirs WGT_SERVER)" "$(theirs GATEWAY_SSH)")
            if [ "$p" = "$PUCK_NAME" ]; then
                [ "$them" != "$me" ] || die "$(shown "$f") is the same gateway as $(shown "$CONF"): one settings file per gateway"
                [ -z "$TPK_LINK" ] || [ "$(theirs TPK_LINK)" != "$TPK_LINK" ] \
                    || die "$p's link to $o also has TPK_LINK $TPK_LINK, and on the puck they must differ: empty it in $CONF, and link picks another"
                continue
            fi
            [ "$them" = "$me" ] || continue
            [ -z "$TPK_LINK" ] || [ "$(theirs TPK_LINK)" != "$TPK_LINK" ] \
                || die "puck '$p' on the same gateway also has TPK_LINK $TPK_LINK: empty it, and link picks a free one"
            [ -z "$PUCK_PORT" ] || [ "$(theirs PUCK_PORT)" != "$PUCK_PORT" ] \
                || die "puck '$p' on the same gateway also uses PUCK_PORT $PUCK_PORT: empty it, and link picks a free one"
            [ "$(theirs TPK_ROUTING)" = "$TPK_ROUTING" ] || continue
            if [ "$TPK_ROUTING" = wgt ]; then mine=$(jump_codes) fix="set JUMP_CODE, JUMP_LAN_CODE or JUMP_LANONLY_CODE"
                ocode=$(jump_code "$(theirs JUMP_CODE)" "$p")
                other="$ocode $(lan_code "$(theirs JUMP_LAN_CODE)" "$(theirs JUMP_LAN)" "$ocode" a)"
                other="$other $(lan_code "$(theirs JUMP_LANONLY_CODE)" "$(theirs JUMP_LANONLY)" "$ocode" l)"
            else mine="$CLIENT_IPS" other="$(theirs CLIENT_IPS)" fix="take it out of one CLIENT_IPS"; fi
            for x in $mine; do
                for y in $other; do
                    [ "$x" != "$y" ] || die "puck '$p' on the same gateway also uses $x: a jump code or client goes to one puck ($fix)"
                done
            done
        done
    done
}

# link_avoid: the TPK_LINK of the puck's links to its other gateways.
link_avoid() {
    for o in $(gateways); do
        [ "$o" != "$GATEWAY_NAME" ] || continue
        # shellcheck disable=SC1090
        ( TPK_LINK=""; . "$(gateway_file "$PUCK_NAME" "$o")"; [ -z "$TPK_LINK" ] || echo "$TPK_LINK" )
    done | tr '\n' ' ' | sed 's/ *$//'
}

# ssh_entry <hostname> <user or empty>: the ~/.ssh/config entry for a puck's
# emergency door, so your own ssh reaches it through cloudflared.
ssh_entry() {
    printf 'Host %s\n' "$1"
    [ -z "$2" ] || printf '    User %s\n' "$2"
    printf '    ProxyCommand %s access ssh --hostname %%h\n' "$(cloudflared_bin)"
}

cmd_ssh_config() {
    require SSH_CLOUDFLARE
    printf '# Add to ~/.ssh/config, or let scripts/tpk-ctl.sh ssh-config install do it.\n\n'
    ssh_entry "$SSH_CLOUDFLARE" "$PUCK_USER"
}

# cmd_ssh_install: every puck's emergency door in ~/.config/tunnel-puck/ssh_config
# (rewritten each time), and one Include line for it at the top of ~/.ssh/config
# (added once, after a backup). Nothing else in ~/.ssh/config changes.
cmd_ssh_install() {
    file="$HOME/.config/tunnel-puck/ssh_config" user_conf="$HOME/.ssh/config"
    include="Include $file"
    mkdir -p "${file%/*}"
    tmp=$(mktemp)
    printf '# tunnel-puck: the pucks'"'"' emergency doors (SSH_CLOUDFLARE), for your own ssh.\n' > "$tmp"
    printf '# Made by scripts/tpk-ctl.sh ssh-config install, rewritten each time.\n' >> "$tmp"
    n=0
    for o in $(pucks); do
        # Each read on its own, in a subshell, so one puck's settings don't leak into the next.
        # shellcheck disable=SC1090
        door=$( (SSH_CLOUDFLARE=""; . "$(puck_file "$o")"; printf '%s' "$SSH_CLOUDFLARE") )
        [ -n "$door" ] || continue    # no emergency door for this puck
        # shellcheck disable=SC1090
        user=$( (SSH_LAN=""; . "$(puck_file "$o")"; user_of "$SSH_LAN") )
        printf '\n# %s\n' "$o" >> "$tmp"
        ssh_entry "$door" "$user" >> "$tmp"
        n=$((n + 1))
    done
    install -m 600 "$tmp" "$file"; rm -f "$tmp"
    log "wrote $file: $n puck(s) with an emergency door"
    mkdir -p "$HOME/.ssh"; chmod 700 "$HOME/.ssh"
    if [ -f "$user_conf" ] && grep -qxF "$include" "$user_conf"; then
        log "$user_conf already includes it"
        return 0
    fi
    if [ -f "$user_conf" ]; then
        backup="$user_conf.$(date +%Y%m%d-%H%M%S).bak"
        cp -p "$user_conf" "$backup"; log "backed up $user_conf to $backup"
    fi
    # At the top: an Include further down could fall inside a Host block, and apply only there.
    tmp=$(mktemp)
    { printf '%s\n\n' "$include"; [ ! -f "$user_conf" ] || cat "$user_conf"; } > "$tmp"
    install -m 600 "$tmp" "$user_conf"; rm -f "$tmp"
    log "added '$include' at the top of $user_conf"
}


# --- The puck ------------------------------------------------------------------

cmd_init() {
    if [ -n "$SSH_CLOUDFLARE" ]; then
        [ -r "$CF_TOKEN_FILE" ] || die "SSH_CLOUDFLARE is set but CF_TOKEN_FILE ($CF_TOKEN_FILE) isn't readable: do step 1.2b first, or empty SSH_CLOUDFLARE"
        CF_TOKEN=$(tr -d ' \n\r' < "$CF_TOKEN_FILE")
        [ -n "$CF_TOKEN" ] || die "CF_TOKEN_FILE ($CF_TOKEN_FILE) is empty"
    fi
    pick_puck
    run puck puck.sh init
    CF_TOKEN=""
}

# read_password <prompt>: from the terminal without echo, or one line of stdin
# when it isn't a terminal (tpk-ctl.sh wifi add name < file). Never an argument.
read_password() {
    if [ -t 0 ]; then
        printf '%s' "$1" >&2
        stty -echo; IFS= read -r pw || pw=""; stty echo; echo >&2
    else
        IFS= read -r pw || pw=""
    fi
}

valid_psk() { case "${#1}" in 0|[89]|[1-5][0-9]|6[0-3]) return 0 ;; esac; return 1; }    # empty: open

# wifi_list: this puck's Wi-Fi list, pucks/<puck>/wifi.conf, checked: "name::password"
# lines (password empty: an open network), most preferred first; # starts a comment.
# Kept mode 600: it holds passwords.
wifi_list() {
    file=$(wifi_file "$PUCK_NAME")
    [ -r "$file" ] || die "no Wi-Fi list for $PUCK_NAME: cp $(shown "$EXAMPLES")/puck.wifi.conf.example $(shown "$file") (or tpk-ctl.sh new makes it), and fill it in"
    chmod 600 "$file"
    n=0
    while IFS= read -r line || [ -n "$line" ]; do
        n=$((n + 1))
        case "$line" in
            ''|'#'*) continue ;;
            *::*) ;;
            *) die "$where line $n: want name::password (password empty for an open network)" ;;
        esac
        [ -n "${line%%::*}" ] || die "$where line $n: the network name is empty"
        valid_psk "${line#*::}" || die "$where line $n: a Wi-Fi password is 8 to 63 characters"
        printf '%s\n' "$line"
    done < "$file"
}

cmd_wifi() {
    case "${1:-list}" in
        list)
            install_exit_script; run puck puck.sh wifi-list ;;
        add)
            WIFI_SSID="${2:-}"; [ -n "$WIFI_SSID" ] || die "tpk-ctl.sh wifi add <network name>"
            if [ -z "$DRY" ]; then
                read_password "Password for '$WIFI_SSID' (empty for an open network): "
                valid_psk "$pw" || die "a Wi-Fi password is 8 to 63 characters"
                WIFI_PSK="$pw"; pw=""
            fi
            install_exit_script; run puck puck.sh wifi-add
            WIFI_PSK="" ;;
        remove)
            WIFI_SSID="${2:-}"; [ -n "$WIFI_SSID" ] || die "tpk-ctl.sh wifi remove <network name>|all"
            install_exit_script; run puck puck.sh wifi-remove ;;
        sync)
            WIFI_LIST=$(wifi_list)
            install_exit_script; run puck puck.sh wifi-sync
            WIFI_LIST="" ;;
        *) die "tpk-ctl.sh wifi [list] | add <name> | remove <name>|all | sync" ;;
    esac
}

# auto_wifi: in all, the puck's Wi-Fi follows its list when there is one; with
# none, the puck's Wi-Fi stays as it is.
auto_wifi() {
    w=$(wifi_file "$PUCK_NAME")
    if [ -e "$w" ]; then log "puck: Wi-Fi from $(shown "$w")"; cmd_wifi sync
    else log "no $(shown "$w"): the puck's Wi-Fi is left as it is"; fi
}

# cmd_ssh [<command>]: a shell on the puck (or one command), the way pick_puck found.
ssh_tty() { ssh -t "$@"; }
cmd_ssh() {
    if [ -t 0 ]; then puck_ssh ssh_tty "$@"    # a terminal only when there's one to give it
    else puck_ssh ssh "$@"; fi
}

# cmd_power reboot|shutdown: restart the puck or power it off. The ssh session ends
# with it, so its exit status says nothing; a reboot then waits for the puck.
cmd_power() {
    if [ "$1" = shutdown ] && [ -z "$DRY" ] && [ "${TPK_YES:-}" != 1 ]; then
        ask_yes "Power off $PUCK_NAME? Away from home, someone must unplug and replug it to bring it back." \
            || die "stopped; the puck is still on"
    fi
    if [ "$1" = reboot ]; then what=reboot; else what=poweroff; fi
    if [ -n "$DRY" ]; then log "[dry-run] on $PUCK: systemctl $what"; return 0; fi
    to puck "$AS_ROOT systemctl $what" 2>/dev/null || true
    if [ "$1" = shutdown ]; then
        log "$PUCK_NAME is shutting down: unplug it once its activity light has stopped (about 10 seconds)"
        return 0
    fi
    log "$PUCK_NAME is rebooting; waiting for it to answer (up to 3 minutes)"
    sleep 15
    i=0
    while [ "$i" -lt 33 ]; do
        if probe_puck; then log "$PUCK_NAME is back"; return 0; fi
        sleep 5; i=$((i + 1))
    done
    die "$PUCK_NAME didn't answer within 3 minutes: try scripts/tpk-ctl.sh $PUCK_NAME test in a while"
}


# --- One gateway -------------------------------------------------------------------

# result <KEY> <a script's output>: the value of its last KEY=<value> line.
result() { printf '%s\n' "$2" | sed -n "s/^$1=//p" | tail -n 1; }

cmd_link() {
    gateway_config >/dev/null
    install_exit_script
    # The subnet first: the puck's interfaces are named from it. What the gateway picks
    # for an empty TPK_LINK or PUCK_PORT is kept in the gateway's settings for good.
    if [ -z "$TPK_LINK" ] || [ -z "$PUCK_PORT" ]; then
        TPK_LINK_AVOID=$(link_avoid)
        confirm_gateway "the link $LINK_IF"
        avoid=""; [ -z "$TPK_LINK_AVOID" ] || avoid=" (not $TPK_LINK_AVOID: the puck's other links)"
        log "gateway: choosing the link's subnet and port$avoid"
        out=$(run gateway gateway.sh pick)
        picked=$(result LINK "$out")
        if [ -n "$picked" ] && [ "$picked" != "$TPK_LINK" ]; then TPK_LINK="$picked"; save TPK_LINK "$TPK_LINK"; link_ids; fi
        picked=$(result PORT "$out")
        if [ -n "$picked" ] && [ "$picked" != "$PUCK_PORT" ]; then PUCK_PORT="$picked"; save PUCK_PORT "$PUCK_PORT"; fi
        if [ -n "$DRY" ] && [ -z "$TPK_LINK" ]; then
            host=0; while case " $TPK_LINK_AVOID " in *" 10.99.0.$host/30 "*) true ;; *) false ;; esac; do host=$((host + 4)); done
            TPK_LINK="10.99.0.$host/30"; link_ids; log "[dry-run] the gateway would pick TPK_LINK (say $TPK_LINK) and PUCK_PORT if empty"
        fi
    fi
    log "puck: WireGuard and its key ($PK_IF)"
    PUCK_PUBKEY=$(result PUBKEY "$(run puck puck.sh link)")
    [ -n "$PUCK_PUBKEY" ] || [ -n "$DRY" ] || die "the puck didn't report its public key"
    routing=""; [ "$TPK_ROUTING" != tpk ] || [ -z "$CLIENT_IPS" ] || routing=", routing and a kill switch for $CLIENT_IPS"
    confirm_gateway "the link $LINK_IF$routing"
    log "gateway: the link $LINK_IF"
    out=$(run gateway gateway.sh link)
    GATEWAY_PUBKEY=$(result PUBKEY "$out")
    [ -n "$GATEWAY_PUBKEY" ] || [ -n "$DRY" ] || die "the gateway didn't report its public key"
    [ -n "$GATEWAY_PUBKEY" ] || GATEWAY_PUBKEY=dry-run
    log "puck: the link ($PK_IF)"
    run puck puck.sh link >/dev/null
    forget_link_key    # a rebuilt link may lead to a reflashed puck: learn its key afresh
    log "waiting for the first handshake"
    [ -n "$DRY" ] || sleep 8
    run puck puck.sh test-link || log "the link isn't fully up yet: run tpk-ctl.sh $PUCK_NAME.$GATEWAY_NAME test in a minute"
}

# jump_owner <code> <port>: is the gateway's jump <code> this puck's? Prints "none"
# (no such jump), "ours" (it points at this puck's link, port <port>), or the
# endpoint it points at instead.
jump_owner() {
    ep=$(wgt_gateway jump list 2>/dev/null | awk -v c="$1" '$1 == c {print $NF; exit}')
    if [ -z "$ep" ]; then echo none
    elif [ "$ep" = "$PUCK_LINK:$2" ]; then echo ours
    else echo "$ep"; fi
}

# guard_jump <code> <port>: stop before touching a jump that belongs to something
# else (another puck, or a jump that isn't a puck at all): wgt jump add on an
# existing code replaces it, and remove deletes it. TPK_TAKEOVER=1 allows it.
guard_jump() {
    [ -z "$DRY" ] || return 0
    owner=$(jump_owner "$1" "$2")
    case "$owner" in none|ours) return 0 ;; esac
    [ "${TPK_TAKEOVER:-}" = 1 ] \
        || die "$GATEWAY_NAME already has a jump '$1', pointing at $owner, not this puck: set JUMP_CODE (or JUMP_LAN_CODE, JUMP_LANONLY_CODE) to another code, or TPK_TAKEOVER=1 to take it over"
    log "jump '$1' points at $owner; taking it over (TPK_TAKEOVER=1)"
}

# jump_port main|lan|lanonly: that rtun's port on the puck (ids.sh).
jump_port() {
    case "$1" in
        lan) echo "$RTUN_LAN_PORT" ;;
        lanonly) echo "$RTUN_LO_PORT" ;;
        *) echo "$RTUN_PORT" ;;
    esac
}

# add_jump <main|lan|lanonly> <code> <label>: the puck's rtun, added to wgt as a jump.
# lanonly: a routes-only jump (wgt jump add --routes), sending only the puck's LAN
# into it; when it's there already, its routes follow the LAN (wgt jump routes).
add_jump() {
    guard_jump "$2" "$(jump_port "$1")"
    out=$(run puck puck.sh jump "$1")
    file=$(result JUMPFILE "$out")
    lan=""
    if [ "$1" = lanonly ]; then
        lan=$(result LAN "$out")
        [ -n "$lan" ] || [ -n "$DRY" ] || die "the puck reported no LAN (its uplink has no IPv4 address?): the LAN-only jump needs one"
        [ -n "$lan" ] || log "[dry-run] the LAN-only jump's routes: the puck's LAN, as puck.sh jump lanonly reports it"
    fi
    if [ -n "$file" ]; then
        confirm_gateway "wgt jump add $2${lan:+ --routes $lan}"
        log "gateway: wgt jump add $2${lan:+ --routes $lan} (the jump file goes puck -> gateway through a pipe, never shown)"
        if [ -n "$DRY" ]; then log "[dry-run] would pipe $file into wgt jump add $2"; return 0; fi
        if [ -n "$lan" ]; then
            to puck "$AS_ROOT cat $file" | wgt_gateway jump add "$2" - --label "$3" --routes "$lan" --yes
        else
            to puck "$AS_ROOT cat $file" | wgt_gateway jump add "$2" - --label "$3" --yes
        fi || die "wgt jump add $2 failed; the jump file stays on the puck ($file) so tpk-ctl.sh exit can try again with TPK_RENEW=1"
        run puck puck.sh jump-done "$1"
    elif [ -z "$DRY" ]; then
        wgt_gateway jump list | grep -qw -- "$2" \
            || die "the puck's rtun exists but $GATEWAY_NAME has no jump '$2': run TPK_RENEW=1 tpk-ctl.sh $PUCK_NAME.$GATEWAY_NAME exit"
        log "jump '$2' is already on $GATEWAY_NAME"
        [ -z "$lan" ] || follow_lan "$2" "$lan"
    fi
}

# jump_routes <code>: what wgt sends into that jump: cidr[,cidr], or none (a jump
# that takes all traffic); nothing when wgt can't say.
jump_routes() { wgt_gateway jump routes "$1" 2>/dev/null | tail -n 1 | tr -d ' \r'; }

# follow_lan <code> <the puck's LAN>: the LAN-only jump's routes become the LAN, if
# they aren't already (the puck moved to another network).
follow_lan() {
    now=$(jump_routes "$1")
    [ -n "$now" ] || die "wgt jump routes $1 gave nothing (is wgt on $GATEWAY_NAME up to date?)"
    if [ "$now" = "$2" ]; then log "jump '$1' sends $2 into the puck: its LAN"; return 0; fi
    confirm_gateway "wgt jump routes $1 $2"
    log "gateway: jump '$1' sent $now into the puck; its LAN is now $2: wgt jump routes $1 $2"
    wgt_gateway jump routes "$1" "$2" || die "wgt jump routes $1 $2 failed"
}

need_link_here() { [ -n "$LINK_NET" ] || die "no link to $GATEWAY_NAME yet: tpk-ctl.sh $PUCK_NAME.$GATEWAY_NAME link first"; }

cmd_exit() {
    gateway_config >/dev/null
    need_link_here
    if [ "$TPK_ROUTING" = tpk ]; then
        log "puck: DNS for $GATEWAY_NAME's puck-exit clients"
        run puck puck.sh dns
        return 0
    fi
    install_exit_script
    add_jump main "$JUMP_CODE" "$JUMP_LABEL"
    if [ -n "$JUMP_LAN_CODE" ]; then
        require JUMP_LAN_LABEL
        add_jump lan "$JUMP_LAN_CODE" "$JUMP_LAN_LABEL"
    fi
    if [ -n "$JUMP_LANONLY_CODE" ]; then
        require JUMP_LANONLY_LABEL
        add_jump lanonly "$JUMP_LANONLY_CODE" "$JUMP_LANONLY_LABEL"
    fi
    log "next: tpk-ctl.sh $PUCK_NAME.$GATEWAY_NAME client <device> to give a device a config through the puck"
}

cmd_client() {
    [ "$TPK_ROUTING" = wgt ] \
        || die "tpk-ctl.sh client is for TPK_ROUTING=wgt; with tpk, list the config's address in CLIENT_IPS and run tpk-ctl.sh link"
    device="" egress="" lan="" use="tpk-ctl.sh client <device> [--egress] [--lan]"
    for a in "$@"; do
        case "$a" in
            --egress) egress=1 ;;
            --lan) lan=1 ;;
            -*) die "$use" ;;
            *) [ -z "$device" ] || die "$use (one device)"; device="$a" ;;
        esac
    done
    [ -n "$device" ] || die "$use"
    exit_cmd="tpk-ctl.sh $PUCK_NAME.$GATEWAY_NAME exit"
    if [ -z "$lan" ]; then code="$JUMP_CODE"
    elif [ -n "$egress" ]; then
        code="$JUMP_LAN_CODE"
        [ -n "$code" ] || die "no LAN jump for --egress --lan: set JUMP_LAN=\"yes\" in $CONF, then $exit_cmd"
    else
        code="$JUMP_LANONLY_CODE"
        [ -n "$code" ] || die "no LAN-only jump for --lan: set JUMP_LANONLY=\"yes\" in $CONF, then $exit_cmd (--egress --lan: the internet too)"
    fi
    confirm_gateway "wgt client add $device --exit $code"
    wgt_gateway client add "$device" --exit "$code" --warp off --no-qr --yes
    [ "$TPK_DISABLED" != yes ] || log "note: $GATEWAY_NAME is disabled, but this new config works now (disable again to turn it off too)"
    [ "$code" != "$JUMP_LANONLY_CODE" ] || log "$code.$device: the friend's LAN through the puck, the internet from $GATEWAY_NAME"
    log "made $code.$device on $GATEWAY_NAME; wgt-ctl put it in ~/wg-tunnels/devices/$device/config/ (or: bin/wgt-ctl $WGT_SERVER client show $code.$device --qr)"
}

# cmd_mode [--egress] [--lan] [<interface>]: what the gateway's clients reach
# through the puck. --egress: the internet from the friend's IP (the default);
# with --lan, the friend's LAN as well. --lan alone (the LAN, with the internet
# from the gateway) is per device, the LAN-only jump. No flags: print the mode.
cmd_mode() {
    egress="" lan="" ifc=""
    for a in "$@"; do
        case "$a" in
            --egress) egress=1 ;;
            --lan) lan=1 ;;
            -*) die "tpk-ctl.sh mode [--egress] [--lan] [<interface>]" ;;
            *) ifc="$a" ;;
        esac
    done
    need_link_here
    [ -n "$ifc" ] || { if [ "$TPK_ROUTING" = tpk ]; then ifc="$PK_IF"; else ifc="$RTUN_IF"; fi; }
    if [ -z "$egress$lan" ]; then
        printf '%s %s: %s\n' "$GATEWAY_NAME" "$ifc" "$(result MODE "$(run puck puck.sh mode "$ifc")")"
        return 0
    fi
    [ -n "$egress" ] || die "--lan alone (the friend's LAN, with the internet from the gateway) is per device: JUMP_LANONLY=\"yes\" in $CONF, tpk-ctl.sh $PUCK_NAME.$GATEWAY_NAME exit, then tpk-ctl.sh $PUCK_NAME.$GATEWAY_NAME client <device> --lan (--egress --lan here gives both to every client)"
    run puck puck.sh mode "$ifc" "egress${lan:++lan}"
}

# test_way_out: with wgt, do clients leave from the puck's network? wgt's jump
# test traces out through the jump without WARP (the path tunnel-puck's configs
# take), and the IP it sees must be the puck's own public one.
test_way_out() {
    what="clients exit from the puck's network"
    if [ "$TPK_ROUTING" != wgt ]; then printf 'SKIP  %s (tested with TPK_ROUTING=wgt only)\n' "$what"; return 0; fi
    puck_ip=$(to puck "curl -4 -fsS -m 10 $TRACE_URL" 2>/dev/null | sed -n 's/^ip=//p')
    out=$(wgt_gateway jump test "$JUMP_CODE" 2>&1)
    jump_ip=$(printf '%s\n' "$out" | grep "jump $JUMP_CODE: plain traffic exits through the jump" \
        | sed -n 's/.*(ip=\([^ )]*\).*/\1/p')
    if [ -z "$puck_ip" ]; then
        printf 'FAIL  %s: the puck got no answer from %s\n' "$what" "$TRACE_URL"; return 1
    elif [ -z "$jump_ip" ]; then
        printf 'FAIL  %s: wgt jump test %s gave no plain trace (is wgt on %s up to date?):\n' "$what" "$JUMP_CODE" "$GATEWAY_NAME"
        printf '%s\n' "$out" | grep -E 'FAIL|ERROR|error|unexpected' | sed 's/^ */      /'
        return 1
    elif [ "$jump_ip" = "$puck_ip" ]; then
        printf 'PASS  %s: %s (wgt jump test %s, without WARP)\n' "$what" "$jump_ip" "$JUMP_CODE"
    else
        printf 'FAIL  clients exit from %s, not the puck'"'"'s network (%s)\n' "$jump_ip" "$puck_ip"; return 1
    fi
}

# test_lan_routes: the LAN-only jump sends the puck's LAN into it, as it is now.
test_lan_routes() {
    code="$JUMP_LANONLY_CODE" fix="tpk-ctl.sh $PUCK_NAME.$GATEWAY_NAME exit"
    lan=$(result LAN "$(run puck puck.sh lan 2>/dev/null)")
    routes=$(jump_routes "$code")
    if [ -z "$lan" ]; then
        printf 'FAIL  the puck reports no LAN for the LAN-only jump %s (%s installs the current exit script)\n' "$code" "$fix"; return 1
    elif [ -z "$routes" ]; then
        printf 'FAIL  wgt jump routes %s gave nothing: is the jump on %s, and wgt up to date? (%s)\n' "$code" "$GATEWAY_NAME" "$fix"; return 1
    elif [ "$routes" = "$lan" ]; then
        printf 'PASS  the LAN-only jump %s sends the puck'"'"'s LAN into it: %s\n' "$code" "$lan"
    else
        printf 'FAIL  the LAN-only jump %s sends %s into the puck, but its LAN is %s: %s (it updates them)\n' "$code" "$routes" "$lan" "$fix"; return 1
    fi
}

# test_gateway: the loaded gateway: the puck's end of its link, the gateway's, then end to end.
test_gateway() {
    rc_v=0
    if [ -z "$LINK_NET" ]; then
        printf 'FAIL  no link to %s yet (tpk-ctl.sh %s.%s link)\n' "$GATEWAY_NAME" "$PUCK_NAME" "$GATEWAY_NAME"; return 1
    fi
    [ "$TPK_DISABLED" != yes ] || printf 'INFO  %s is disabled: its clients of the puck are off%s (tpk-ctl.sh %s.%s enable)\n' \
        "$GATEWAY_NAME" "${TPK_DISABLED_CONFIGS:+ ($TPK_DISABLED_CONFIGS)}" "$PUCK_NAME" "$GATEWAY_NAME"
    log "puck: the link to $GATEWAY_NAME ($PK_IF)"
    run puck puck.sh test-link || rc_v=1
    log "gateway $GATEWAY_NAME ($GATEWAY_SSH)"
    run gateway gateway.sh test || rc_v=1
    if [ -z "$DRY" ] && [ "$TPK_ROUTING" = wgt ] && [ -n "$JUMP_LANONLY_CODE" ]; then test_lan_routes || rc_v=1; fi
    if [ -z "$DRY" ]; then
        log "end to end through $GATEWAY_NAME (a client's way out; up to 30s while wgt brings the jump up)"
        test_way_out || rc_v=1
    fi
    return "$rc_v"
}

# jump_configs all|on <code>...: the gateway's client configs whose exit is one of these
# jumps (wgt client list --tsv: the config's name, then its exit); on: without the
# ones disabled already (the 4th column, last handshake, says "disabled").
jump_configs() {
    which="$1"; shift
    wgt_gateway client list --tsv 2>/dev/null | awk -F '\t' -v codes=" $* " -v which="$which" \
        'index(codes, " " $2 " ") && (which == "all" || $4 != "disabled") {print $1}'
}

# The puck's configs on the loaded gateway that disable turned off are kept in
# TPK_DISABLED_CONFIGS, so enable turns back on just those (not one you turned off
# yourself with wgt). TPK_DISABLED=yes: disable without a device, the whole gateway.
off_add() { TPK_DISABLED_CONFIGS=$(words "$(printf '%s\n' $TPK_DISABLED_CONFIGS "$@" | grep . | awk '!seen[$0]++')"); save TPK_DISABLED_CONFIGS "$TPK_DISABLED_CONFIGS"; }
off_drop() { TPK_DISABLED_CONFIGS=$(words "$(printf '%s\n' $TPK_DISABLED_CONFIGS | grep -vxF -- "$1" || true)"); save TPK_DISABLED_CONFIGS "$TPK_DISABLED_CONFIGS"; }

# of_device <device> <names...>: those that are the device's configs on the puck's jumps.
of_device() {
    d="$1"; shift
    for n in "$@"; do
        for code in $(jump_codes); do [ "$n" != "$code.$d" ] || echo "$n"; done
    done
}

# cmd_disable [<device>]: the loaded gateway's clients of the puck (or that device's
# configs) off; the puck, its link and its rtun stay up (test still passes), and
# nothing is deleted. wgt: wgt client disable (keys and address kept). tpk: the
# whole gateway only: the link is rebuilt without CLIENT_IPS's routing, so the kill
# switch blocks them.
cmd_disable() {
    device="${1:-}"
    gateway_config >/dev/null
    need_link_here
    if [ "$TPK_ROUTING" = tpk ]; then
        [ -z "$device" ] || die "one device at a time needs TPK_ROUTING=wgt; with tpk, take its address out of CLIENT_IPS (in $CONF) and run link"
        if [ "$TPK_DISABLED" = yes ]; then log "$GATEWAY_NAME is disabled already"; return 0; fi
        confirm_gateway "disable: no routing to the puck for $CLIENT_IPS (the kill switch blocks them)"
        if [ -n "$DRY" ]; then log "[dry-run] would save TPK_DISABLED=\"yes\" and rebuild the link"; return 0; fi
        save TPK_DISABLED yes; TPK_DISABLED=yes
        cmd_link
        log "$GATEWAY_NAME disabled: its puck-exit clients get no internet (tpk-ctl.sh $PUCK_NAME.$GATEWAY_NAME enable)"
        return 0
    fi
    if [ -z "$device" ] && [ "$TPK_DISABLED" = yes ]; then log "$GATEWAY_NAME is disabled already${TPK_DISABLED_CONFIGS:+ ($TPK_DISABLED_CONFIGS)}"; return 0; fi
    # shellcheck disable=SC2046
    names=$(jump_configs on $(jump_codes))
    # shellcheck disable=SC2086
    [ -z "$device" ] || names=$(of_device "$device" $names)
    names=$(words "$names")
    if [ -n "$device" ] && [ -z "$names" ] && [ -z "$DRY" ]; then
        log "$GATEWAY_NAME: $device has no config on the puck's jump that's on (wgt client list)"; return 0
    fi
    confirm_gateway "disable: wgt client disable ${names:-(no configs on the jump yet)}"
    if [ -n "$DRY" ]; then log "[dry-run] would disable ${device:-the puck}'s configs on jump(s) $(jump_codes)"; return 0; fi
    [ -n "$device" ] || save TPK_DISABLED yes
    for c in $names; do
        wgt_gateway client disable "$c" --yes || die "wgt client disable $c failed; the ones before it are off and listed in TPK_DISABLED_CONFIGS"
        off_add "$c"
    done
    log "$GATEWAY_NAME: ${names:-nothing} off; the link and rtun stay up (tpk-ctl.sh $PUCK_NAME.$GATEWAY_NAME enable${device:+ $device})"
}

# cmd_enable [<device>]: the reverse: what disable turned off (that device's, or all
# of it) back on; tpk: the routing back.
cmd_enable() {
    device="${1:-}"
    gateway_config >/dev/null
    need_link_here
    if [ "$TPK_ROUTING" = tpk ]; then
        [ -z "$device" ] || die "one device at a time needs TPK_ROUTING=wgt"
        if [ "$TPK_DISABLED" != yes ]; then log "$GATEWAY_NAME isn't disabled"; return 0; fi
        confirm_gateway "enable: routing to the puck for $CLIENT_IPS"
        if [ -n "$DRY" ]; then log "[dry-run] would clear TPK_DISABLED and rebuild the link"; return 0; fi
        save TPK_DISABLED ""; TPK_DISABLED=""
        cmd_link
        log "$GATEWAY_NAME enabled"
        return 0
    fi
    names="$TPK_DISABLED_CONFIGS"
    # shellcheck disable=SC2086
    [ -z "$device" ] || names=$(words "$(of_device "$device" $names)")
    if [ -z "$names" ] && { [ -n "$device" ] || [ "$TPK_DISABLED" != yes ]; }; then
        log "$GATEWAY_NAME: nothing ${device:+of $device }that disable turned off"; return 0
    fi
    confirm_gateway "enable: wgt client enable ${names:-(none were turned off)}"
    if [ -n "$DRY" ]; then log "[dry-run] would enable $names"; return 0; fi
    for c in $names; do
        wgt_gateway client enable "$c" --yes || die "wgt client enable $c failed; still off: $TPK_DISABLED_CONFIGS"
        off_drop "$c"
    done
    # Anything back on: the gateway isn't wholly off any more (status: partly, or serving).
    [ "$TPK_DISABLED" != yes ] || save TPK_DISABLED ""
    log "$GATEWAY_NAME: ${names:-nothing} back on"
}

# --- Status ----------------------------------------------------------------------

# gateway_state: the loaded gateway's, by what disable turned off: serving, partly or disabled.
gateway_state() {
    if [ "$TPK_DISABLED" = yes ]; then echo disabled
    elif [ -n "$TPK_DISABLED_CONFIGS" ]; then echo partly
    else echo serving; fi
}

# ago <seconds|none>: 45s ago, 12m ago, 3h ago, 14d ago; never.
ago() {
    case "$1" in
        none) echo never ;;    # no handshake (as puck.sh and gateway.sh report it)
        ''|*[!0-9]*) echo "$1" ;;
        *) if [ "$1" -lt 120 ]; then echo "${1}s ago"; elif [ "$1" -lt 7200 ]; then echo "$(( $1 / 60 ))m ago"
           elif [ "$1" -lt 172800 ]; then echo "$(( $1 / 3600 ))h ago"; else echo "$(( $1 / 86400 ))d ago"; fi ;;
    esac
}

# config_rows <CONFIG|CLIENT> <rows> <none>: status's rows under a gateway, with
# the same columns as wgt client list (LAST HANDSHAKE, RX, TX); <none> when empty.
config_rows() {
    if [ -z "$2" ]; then echo "$3"; return 0; fi
    printf '  %-28s %-14s %-7s %s\n%s\n' "$1" "LAST HANDSHAKE" RX TX "$2"
}

# client_lines: gateway.sh clients' "CLIENT <ip> <age>" lines as status shows them.
client_lines() {
    while read -r tag ip age rx tx; do
        [ "$tag" = CLIENT ] || continue
        if [ "$age" = missing ]; then printf '  %-28s no peer on %s with this address\n' "$ip" "$WG_IF"
        elif [ "$TPK_DISABLED" = yes ]; then printf '  %-28s %-14s %-7s %s\n' "$ip" disabled "$(size "$rx")" "$(size "$tx")"
        else printf '  %-28s %-14s %-7s %s\n' "$ip" "$(ago "$age")" "$(size "$rx")" "$(size "$tx")"; fi
    done
}

# size <bytes>: short, as wgt client list shows it: 0, 512B, 330K, 13M, 1.2G.
size() {
    awk -v n="${1:-0}" 'BEGIN {
        if (n >= 1073741824) { v = n / 1073741824; u = "G" } else if (n >= 1048576) { v = n / 1048576; u = "M" }
        else if (n >= 1024) { printf "%.0fK", n / 1024; exit } else { print (n > 0 ? n "B" : "0"); exit }
        printf (v < 10 ? "%.1f%s" : "%.0f%s"), v, u }'
}

# cmd_status: at a glance, read-only and quick (no jump is brought up): the puck
# (how it was reached, uplink, Wi-Fi, public IP), then per gateway whether it's
# serving, the link's and rtun's last handshakes (from the puck), the jump's state
# and the configs on it, with each one's last handshake (from wgt). test says why.
cmd_status() {
    reset_gateway
    st=$(run puck puck.sh status 2>/dev/null || true)
    get() { printf '%s\n' "$st" | sed -n "s/^$1=//p" | head -n 1; }
    hs() { a=$(printf '%s\n' "$st" | awk -v i="$1" '$1 == "HS" && $2 == i {print $3}'); [ -n "$a" ] || a="missing"; ago "$a"; }
    wifi=$(get WIFI)
    printf '%s: via %s\n' "$PUCK_NAME" "$PUCK_VIA"
    lan=$(get LAN)
    printf '  uplink %s%s, public IP %s, saved Wi-Fi: %s\n' "$(get UPLINK)" "${lan:+ (LAN $lan)}" "$(get IP)" "${wifi:-none}"
    names=$(the_gateways)
    [ -n "$names" ] || { echo "  serves no gateway yet (tpk-ctl.sh $PUCK_NAME gateway add <gateway>)"; return 0; }
    printf '%-10s %-9s %-20s %-21s %s\n' GATEWAY STATE LINK RTUN JUMP
    gap=""
    for v in $names; do
        printf '%s' "$gap"; gap="
"    # a blank line between gateways
        QUIET=1 load_gateway "$v"
        if [ -z "$LINK_NET" ]; then printf '%-10s %s\n' "$v" "no link yet (tpk-ctl.sh $PUCK_NAME.$v link)"; continue; fi
        state=$(gateway_state)
        link="$PK_IF $(hs "$PK_IF")"
        if [ "$TPK_ROUTING" = wgt ]; then
            rtun="$RTUN_IF $(hs "$RTUN_IF")"
            jump=$(wgt_gateway jump list 2>/dev/null | awk -v c="$JUMP_CODE" '$1 == c' \
                | grep -oE '(up, no WARP|partly up|idle|up)( \([0-9]+ active\))?' | head -n 1)
            jump="$JUMP_CODE ${jump:-(not on $v)}"
            configs=$(wgt_gateway client list --tsv 2>/dev/null | awk -F '\t' -v codes=" $(jump_codes) " \
                'index(codes, " " $2 " ") {split($5, t, "/"); printf "  %-28s %-14s %-7s %s\n", $1, $4, t[1], t[2]}')
            configs=$(config_rows CONFIG "$configs" "  no configs on the jump yet (tpk-ctl.sh $PUCK_NAME.$v client <device>)")
        else
            rtun="-" jump="-"
            configs=$(config_rows CLIENT "$(run gateway gateway.sh clients 2>/dev/null | client_lines)" \
                "  clients (CLIENT_IPS): ${CLIENT_IPS:-none}")
        fi
        printf '%-10s %-9s %-20s %-21s %s\n' "$v" "$state" "$link" "$rtun" "$jump"
        printf '%s\n' "$configs"
    done
}


# status_watch <bell> [seconds]: status, redrawn every <seconds> (default 5) until
# Ctrl-C. Each round is a fresh status (a few ssh calls), drawn once it's all in.
# bell (status --alert): the terminal's bell when a config (or tpk client) comes
# on, its last handshake under 3 minutes after it wasn't, with a line saying which.
status_watch() {
    bell="$1" every="${2:-5}"
    case "$every" in ''|*[!0-9]*|0) die "status --watch|--alert [seconds]: a whole number of seconds" ;; esac
    trap 'printf "\033[?1049l"; exit 0' INT TERM
    printf '\033[?1049h'
    was="" first=1 events=""
    while :; do
        out=$(cmd_status 2>&1)
        ring=""
        if [ -n "$bell" ]; then
            now=$(printf '%s\n' "$out" | active_names)
            if [ -z "$first" ]; then
                stamp=$(date +%H:%M:%S)
                for n in $now; do
                    case " $was " in *" $n "*) ;; *) ring=1 events="$stamp $n connected
$events"; notify "$n connected ($PUCK_NAME)" ;; esac
                done
                for n in $was; do
                    case " $now " in *" $n "*) ;; *) events="$stamp $n went idle
$events" ;; esac
                done
                events=$(printf '%s\n' "$events" | head -n 5)   # the newest five
            fi
            was=$(words "$now") first=""
        fi
        printf '\033[H\033[2J%s\n%s\n\033[2mtpk-ctl.sh %s status, %s: every %ss%s  Ctrl-C quits\033[0m\n%s' \
            "$out" "$(printf '%s' "$events" | sed 's/^/  /')" "$PUCK_NAME${GATEWAY_PICK:+.$GATEWAY_PICK}" \
            "$(date +%H:%M:%S)" "$every" "${bell:+, bell on}" "${ring:+$(printf '\a')}"
        sleep "$every"
    done
}

# notify <text>: a desktop notification on this computer, for status --alert:
# macOS (with a sound) or Linux (notify-send); nothing elsewhere (the bell rings).
notify() {
    if command -v osascript >/dev/null 2>&1; then
        osascript -e 'on run argv' -e 'display notification (item 1 of argv) with title "tpk-ctl" sound name "Glass"' \
            -e 'end run' "$1" >/dev/null 2>&1 &
    elif command -v notify-send >/dev/null 2>&1; then
        notify-send tpk-ctl "$1" >/dev/null 2>&1 &
    fi
}

# active_names: from status's lines, the configs (or tpk clients) in use: a last
# handshake under 3 minutes (ago prints seconds under 2 minutes, then 2m).
active_names() {
    awk '/last handshake: ([0-9]+s|2m) ago/ {print $1}'
}


# undo_gateway: take the loaded gateway's part away, all of it: with wgt the configs made
# through the puck's jumps (tpk-ctl.sh client made them; without the jump they're
# cruft), then the jumps; then the link on both ends. Gone for good: a config
# made again gets new keys (wgt-ctl keeps your old copy as a .bak).
undo_gateway() {
    route_around "$GATEWAY_NAME"
    jumps="" configs=""
    if [ "$TPK_ROUTING" = wgt ] && [ -n "$LINK_NET" ]; then
        for pair in "$JUMP_CODE main" "$JUMP_LAN_CODE lan" "$JUMP_LANONLY_CODE lanonly"; do
            # shellcheck disable=SC2086
            set -- $pair; [ $# -eq 2 ] || continue    # that LAN jump is off
            if [ -n "$DRY" ]; then jumps="$jumps $1"; continue; fi
            case "$(jump_owner "$1" "$(jump_port "$2")")" in
                none) ;;    # already gone
                ours) jumps="$jumps $1" ;;
                *) log "leaving jump '$1': it isn't this puck's" ;;
            esac
        done
        # shellcheck disable=SC2086
        [ -z "$jumps" ] || [ -n "$DRY" ] || configs=$(words "$(jump_configs all $jumps)")
    fi
    what="remove"
    if [ -n "$DRY" ] && [ -n "$jumps" ]; then what="$what the configs on jump(s)$jumps (none listed in a dry run), then"
    elif [ -n "$configs" ]; then what="$what the configs $configs, then"; fi
    [ -z "$jumps" ] || what="$what jump(s)$jumps,"
    killswitch=""; [ "$TPK_ROUTING" != tpk ] || killswitch=" and its kill switch"
    confirm_gateway "$what the link $LINK_IF$killswitch"
    [ -z "$DRY" ] || log "[dry-run] would $what the link $LINK_IF$killswitch"
    for c in $configs; do
        log "$GATEWAY_NAME: wgt client remove $c"
        wgt_gateway client remove "$c" --yes || die "wgt client remove $c failed; the jump and link are still there"
    done
    for j in $jumps; do
        log "$GATEWAY_NAME: wgt jump remove $j"
        wgt_gateway jump remove "$j" --yes || die "wgt jump remove $j failed; the link is still there"
    done
    run gateway gateway.sh undo
    if [ -n "$LINK_NET" ]; then run puck puck.sh undo-link; fi
}


# --- Several gateways ---------------------------------------------------------------

cmd_gateway() {
    sub="${1:-list}"; [ $# -eq 0 ] || shift
    case "$sub" in
        add) cmd_gateway_add "${1:-}" ;;
        list) cmd_gateway_list ;;
        remove) cmd_gateway_remove "${1:-}" ;;
        *) die "tpk-ctl.sh $PUCK_NAME gateway add <gateway> | list | remove <gateway>" ;;
    esac
}

# cmd_gateway_add <gateway>: serve one more gateway. Its settings file is made if missing: a
# wgt-ctl server of that name gets TPK_ROUTING=wgt, ready to go; any other name a
# tpk file to fill in first. Then link, exit and test for that gateway alone.
cmd_gateway_add() {
    name="$1"
    [ -n "$name" ] || die "tpk-ctl.sh $PUCK_NAME gateway add <gateway>: a wgt-ctl server's name (bin/wgt-ctl list), or a name of your own for a TPK_ROUTING=tpk gateway"
    gateway_name_ok "$name"
    f=$(gateway_file "$PUCK_NAME" "$name") example="$EXAMPLES/puck.gateway.conf.example"
    if [ -e "$f" ]; then
        log "$(shown "$f") exists: using it"
    elif [ -n "$DRY" ]; then
        die "a dry run doesn't make settings files: make $(shown "$f") first (from $(shown "$EXAMPLES")/puck.gateway.conf.example)"
    elif wgt_servers 2>/dev/null | grep -qxF -- "$name"; then
        private_dir "${f%/*}"
        (umask 077; sed -e 's/^TPK_ROUTING="tpk"/TPK_ROUTING="wgt"/' -e "s/^WGT_SERVER=\"\"/WGT_SERVER=\"$name\"/" "$example" > "$f")
        log "made $(shown "$f"): TPK_ROUTING=wgt, WGT_SERVER=$name (wgt-ctl's server)"
    else
        private_dir "${f%/*}"
        (umask 077; cp "$example" "$f")
        log "made $(shown "$f"). $name isn't in wgt-ctl's list (bin/wgt-ctl list), so it starts as TPK_ROUTING=tpk:"
        log "fill in GATEWAY_SSH, GATEWAY_HOST and CLIENT_IPS (or TPK_ROUTING=\"wgt\" and WGT_SERVER), then run this again"
        return 0
    fi
    load_gateway "$name"
    pick_puck
    cmd_link
    cmd_exit
    rc=0; test_gateway || rc=1
    if [ "$rc" = 0 ]; then log "$PUCK_NAME serves $name: all tests passed"
    else log "some tests failed: see the README's Troubleshooting"; fi
    return "$rc"
}

# cmd_gateway_list: the gateways the puck serves, from their settings files (test says
# whether they're up).
cmd_gateway_list() {
    names=$(gateways)
    if [ -z "$names" ]; then echo "$PUCK_NAME serves no gateway yet: tpk-ctl.sh $PUCK_NAME gateway add <gateway>"; return 0; fi
    printf '%-12s %-8s %-8s %-24s %-15s %-26s %s\n' GATEWAY ROUTING STATE SERVER LINK "ON THE PUCK" JUMPS
    for v in $names; do
        ( QUIET=1; load_gateway "$v" 2>/dev/null
          if [ "$TPK_ROUTING" = wgt ]; then server="$WGT_SERVER"; jumps=$(jump_codes); ifs="$PK_IF $(rtun_ifs)"
          else server="$GATEWAY_SSH"; jumps="-"; ifs="$PK_IF"; fi
          [ -n "$LINK_NET" ] || ifs="(no link yet)"
          state=$(gateway_state); [ -n "$LINK_NET" ] || state=-
          printf '%-12s %-8s %-8s %-24s %-15s %-26s %s\n' "$v" "$TPK_ROUTING" "$state" "$server" "${TPK_LINK:--}" "$ifs" "$jumps" ) \
            || printf '%-12s (its settings file has a problem: tpk-ctl.sh %s.%s config)\n' "$v" "$PUCK_NAME" "$v"
    done
}

# cmd_gateway_remove <gateway>: stop serving it: its jumps, its link on both ends, then its
# settings file (other gateways are untouched).
cmd_gateway_remove() {
    [ -n "$1" ] || die "tpk-ctl.sh $PUCK_NAME gateway remove <gateway> (its gateways: $(words "$(gateways)"))"
    load_gateway "$1"
    pick_puck "$1"
    undo_gateway
    if [ -n "$DRY" ]; then log "[dry-run] would remove $(shown "$CONF")"; return 0; fi
    rm -f "$CONF"
    log "$PUCK_NAME no longer serves $1; removed $(shown "$CONF")"
}


# --- The whole puck --------------------------------------------------------------

cmd_test() {
    rc=0
    log "puck ($PUCK)"
    reset_gateway
    run puck puck.sh test || rc=1
    for v in $(the_gateways); do load_gateway "$v"; test_gateway || rc=1; done
    if [ -n "$DRY" ]; then log "(dry run: nothing was tested)"
    elif [ "$rc" = 0 ]; then log "all tests passed"
    else log "some tests failed: see the README's Troubleshooting"; fi
    return "$rc"
}

# each_gateway <function> [<args>]: run it for each gateway (or the one named), loaded in turn.
each_gateway() {
    fn="$1"; shift
    names=$(the_gateways)
    [ -n "$names" ] || die "$PUCK_NAME serves no gateway yet: tpk-ctl.sh $PUCK_NAME gateway add <gateway>"
    for v in $names; do load_gateway "$v"; "$fn" "$@"; done
}

link_and_exit() { cmd_link; cmd_exit; }
cmd_all() {
    each_gateway link_and_exit
    reset_gateway
    auto_wifi
    cmd_test
}

# cmd_undo [all]: every gateway's part, then the puck's own (all: cloudflared too).
# The settings files stay, so the steps can build it all again.
cmd_undo() {
    what="${1:-}"
    for v in $(gateways); do load_gateway "$v"; undo_gateway; done
    reset_gateway
    run puck puck.sh undo "$what"
}


# --- Main ----------------------------------------------------------------------

main() {
    case "${1:-}" in ''|-h|--help|help) usage; exit 0 ;; esac
    if [ "$1" = new ]; then shift; cmd_new "$@"; exit 0; fi
    if [ "$1" = ssh-config ] && [ "${2:-}" = install ]; then cmd_ssh_install; exit 0; fi
    DRY="${TPK_DRY_RUN:-}"

    # Which puck (and gateway): a first word that isn't a command, <puck> or <puck>.<gateway>; else ask.
    GATEWAY_PICK=""
    if ! is_command "$1"; then
        case "$1" in
            *.*) PUCK_NAME="${1%%.*}" GATEWAY_PICK="${1#*.}" ;;
            *) PUCK_NAME="$1" ;;
        esac
        shift
    else
        ask_puck "$1"
        PUCK_NAME="$PICK"
    fi
    load_puck

    cmd="${1:-}"; [ $# -eq 0 ] || shift
    case "$cmd" in
        config) cmd_config ;;
        ssh-config) cmd_ssh_config ;;
        init) cmd_init ;;
        gateway) cmd_gateway "$@" ;;
        link) one_gateway link; pick_puck; cmd_link ;;
        exit) one_gateway exit; pick_puck; cmd_exit ;;
        client) one_gateway client; cmd_client "$@" ;;
        mode) one_gateway mode; pick_puck; cmd_mode "$@" ;;
        disable|enable) pick_puck; each_gateway "cmd_$cmd" "$@" ;;
        status) pick_puck
            case "${1:-}" in
                --watch) shift; status_watch "" "$@" ;;
                --alert) shift; status_watch bell "$@" ;;
                *) cmd_status ;;
            esac ;;
        wifi) pick_puck; cmd_wifi "$@" ;;
        test) pick_puck; cmd_test ;;
        ssh) pick_puck; cmd_ssh "$@" ;;
        reboot|shutdown) pick_puck; cmd_power "$cmd" ;;
        all) pick_puck; cmd_all ;;
        undo)
            [ -z "$GATEWAY_PICK" ] || die "undo takes the whole puck down; for one gateway: tpk-ctl.sh $PUCK_NAME gateway remove $GATEWAY_PICK"
            pick_puck; cmd_undo "$@" ;;
        *) usage; exit 2 ;;
    esac
}

main "$@"
