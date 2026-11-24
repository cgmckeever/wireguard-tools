#!/bin/sh
# tpk-ctl.sh: set up and run tunnel-puck from your computer (macOS or Linux). Reads
# a puck's settings from ../configs/<puck>.conf (or $TPK_CONF) and runs puck.sh on
# the puck and vps.sh on the VPS over ssh. Without <puck>: the only one there is,
# else it asks (TPK_PUCK=<puck> is the default, and the answer with no terminal).
#
#   tpk-ctl.sh new [<puck>] [--wifi]        a new puck's settings file (asks the name; --wifi: its Wi-Fi list too)
#   tpk-ctl.sh [<puck>] config              show the settings and check they're filled in
#   tpk-ctl.sh [<puck>] ssh-config          print the ~/.ssh/config entry for SSH_CLOUDFLARE
#   tpk-ctl.sh ssh-config install           add every puck's entry to your ssh config (its own file, one Include)
#   tpk-ctl.sh [<puck>] base                1.4, on the puck: packages, updates, watchdog (and cloudflared, with SSH_CLOUDFLARE)
#   tpk-ctl.sh [<puck>] link                2.1: the link (puck and VPS), keys exchanged for you
#   tpk-ctl.sh [<puck>] exit                2.1, by TPK_ROUTING: tpk's DNS resolver, or the wgt jump(s)
#   tpk-ctl.sh [<puck>] client <device> [lan]   2.2, TPK_ROUTING=wgt: a config for <device> through the puck (lan: the LAN jump)
#   tpk-ctl.sh [<puck>] lan-mode isolated|lan [<interface>]   2.3: switch LAN mode (default: wgpk or wgrtun)
#   tpk-ctl.sh [<puck>] wifi [list]         the puck's saved Wi-Fi networks, and its uplink now
#   tpk-ctl.sh [<puck>] wifi add <name>     save a Wi-Fi network on the puck (asks for the password)
#   tpk-ctl.sh [<puck>] wifi remove <name>|all   remove saved Wi-Fi (all: ethernet only)
#   tpk-ctl.sh [<puck>] wifi sync           the puck's Wi-Fi becomes configs/<puck>.wifi.conf, in its order
#   tpk-ctl.sh [<puck>] test                PASS/FAIL on the puck and the VPS (wgt: and a client's way out)
#   tpk-ctl.sh [<puck>] ssh [<command>]     a shell (or a command) on the puck, the first way that answers
#   tpk-ctl.sh [<puck>] reboot              restart the puck, and wait until it answers again
#   tpk-ctl.sh [<puck>] shutdown            power the puck off, safe to unplug (asks first)
#   tpk-ctl.sh [<puck>] all                 link, exit, wifi sync (with a <puck>.wifi.conf), test (after base)
#   tpk-ctl.sh [<puck>] undo [all]          remove it from the VPS and the puck (all: cloudflared too)
#
# The puck is reached the first way that answers: SSH_LAN (at home), then through
# the VPS over the link, then SSH_CLOUDFLARE (the emergency door, for when the
# link is down). Anything that changes the VPS asks first; TPK_YES=1 answers yes
# (only once the person has agreed to these changes on this VPS). TPK_DRY_RUN=1
# shows what would run and changes nothing.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
CONFIGS=$(cd "$HERE/../configs" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
WGT_CTL="$REPO/bin/wgt-ctl"
COMMANDS="new config ssh-config base link exit client lan-mode wifi test ssh reboot shutdown all undo"
# Cloudflare's trace (ip=... lines), as wgt's jump test uses (wgt/const.py TRACE_URL).
TRACE_URL="https://www.cloudflare.com/cdn-cgi/trace"
# The remote prefix that runs a command as root (sudo unless already root).
AS_ROOT='if [ "$(id -u)" = 0 ]; then S=; else S=sudo; fi; $S'


# --- Small helpers -------------------------------------------------------------

log() { printf '==> %s\n' "$*" >&2; }
die() { printf 'tpk-ctl.sh: %s\n' "$*" >&2; exit 1; }
usage() {
    sed -n '2,33s/^# \{0,1\}//p' "$0"
    printf '\nconfigured pucks: %s\n' "$(pucks | tr '\n' ' ' | grep . || echo 'none yet (scripts/tpk-ctl.sh new)')"
}

q() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }    # the value quoted for sh
placeholder() { case "$1" in ''|*example.com*|*'<'*) return 0 ;; esac; return 1; }
is_command() { case " $COMMANDS " in *" $1 "*) return 0 ;; esac; return 1; }
have_tty() { (: < /dev/tty) 2>/dev/null; }

# ask_yes <question>: true on y/yes, asked on the terminal.
ask_yes() {
    printf '%s [y/N] ' "$1" >&2
    read -r yes < /dev/tty || yes=""
    case "$yes" in y|Y|yes) return 0 ;; esac
    return 1
}

# require <SETTING>...: each is set, and not still the example's value.
require() {
    for v in "$@"; do
        eval "val=\${$v}"
        ! placeholder "$val" || die "$v in $CONF is empty or still the example"
    done
}

# save <SETTING> <value>: set SETTING="value" in this puck's settings file
# (replacing its line, or adding one), and say so.
save() {
    tmp=$(mktemp)
    awk -v k="$1" -v line="$1=\"$2\"" 'index($0, k "=") == 1 && !done {print line; done = 1; next} {print}
        END {if (!done) print line}' "$CONF" > "$tmp"
    cat "$tmp" > "$CONF"; rm -f "$tmp"
    log "saved $1=\"$2\" in $CONF"
}


# --- Which puck ----------------------------------------------------------------

# pucks: the configured pucks, one name per line. configs/<puck>.conf; a name with a
# dot, like <puck>.wifi.conf, is a puck's Wi-Fi list, not a puck.
pucks() {
    for f in "$CONFIGS"/*.conf; do
        [ -e "$f" ] || continue
        n=$(basename "$f" .conf)
        case "$n" in *.*) ;; *) echo "$n" ;; esac
    done
    return 0
}

# ask_puck <command>: PICK, when the command line names no puck. One configured:
# that one. Several: a numbered list, TPK_PUCK the default for Enter; with no
# terminal, TPK_PUCK itself, else stop.
ask_puck() {
    names=$(pucks); count=$(printf '%s\n' "$names" | grep -c .)
    default="${TPK_PUCK:-}"
    if [ -n "$default" ] && ! printf '%s\n' "$names" | grep -qx -- "$default"; then
        die "TPK_PUCK=$default, but there's no configs/$default.conf (configured: $(echo $names))"
    fi
    case "$count" in
        0) die "no settings yet: scripts/tpk-ctl.sh new, then fill it in" ;;
        1) PICK="$names"; return 0 ;;
    esac
    if ! have_tty; then
        [ -n "$default" ] || die "which puck? tpk-ctl.sh <puck> $1, or TPK_PUCK=<puck> (configured: $(echo $names))"
        PICK="$default"; return 0
    fi
    printf 'Which puck?\n' >&2
    i=0
    for n in $names; do
        i=$((i + 1))
        if [ "$n" = "$default" ]; then printf '  %d) %s  (default)\n' "$i" "$n" >&2; else printf '  %d) %s\n' "$i" "$n" >&2; fi
    done
    printf '[number or name%s]: ' "${default:+, Enter for $default}" >&2
    read -r answer < /dev/tty || answer=""
    [ -n "$answer" ] || answer="$default"
    case "$answer" in
        '') die "no puck chosen" ;;
        *[!0-9]*) PICK="$answer" ;;
        *) PICK=$(printf '%s\n' "$names" | sed -n "${answer}p") ;;
    esac
    printf '%s\n' "$names" | grep -qx -- "$PICK" || die "no puck '$answer' (configured: $(echo $names))"
}

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
    # shellcheck source=ids.sh
    ( . "$HERE/ids.sh"; PUCK_NAME="$name"; TPK_LINK=""; link_ids ) || exit 1
    ! is_command "$name" || die "'$name' is a command word: pick another name"
    [ ! -e "$CONFIGS/$name.conf" ] || die "configs/$name.conf already exists"
    cp "$CONFIGS/puck.conf.example" "$CONFIGS/$name.conf"
    log "made configs/$name.conf: fill it in, then scripts/tpk-ctl.sh $name config"
    if [ -z "$wifi" ] && have_tty && [ ! -e "$CONFIGS/$name.wifi.conf" ]; then
        ! ask_yes "A Wi-Fi list for it too (configs/$name.wifi.conf)?" || wifi=1
    fi
    if [ -n "$wifi" ]; then
        [ ! -e "$CONFIGS/$name.wifi.conf" ] || die "configs/$name.wifi.conf already exists"
        (umask 077; cp "$CONFIGS/puck.wifi.conf.example" "$CONFIGS/$name.wifi.conf")
        log "made configs/$name.wifi.conf: one name::password per line, most preferred first"
    fi
}


# --- Settings ------------------------------------------------------------------

# load_settings: read CONF and fill in what follows from it.
load_settings() {
    PUCK_NAME=$(basename "$CONF" .conf)
    if [ "$CONF" = "$CONFIGS/$PUCK_NAME.conf" ]; then log "puck: $PUCK_NAME (configs/$PUCK_NAME.conf)"
    else log "puck: $PUCK_NAME ($CONF)"; fi
    # shellcheck disable=SC1090
    . "$CONF"
    : "${SSH_LAN:=}" "${SSH_CLOUDFLARE:=}" "${CF_TOKEN_FILE:=}" "${TPK_ROUTING:=tpk}" "${PUCK_PORT:=}" "${TPK_LINK:=}"
    : "${VPS_SSH:=}" "${VPS_HOST:=}" "${CLIENT_IPS:=}" "${CLIENT_IP6S:=}" "${WG_IF:=wg0}" "${WG_SUBNET:=}"
    : "${WGT_SERVER:=}" "${JUMP_CODE:=}" "${JUMP_LABEL:=}" "${JUMP_LAN:=}" "${JUMP_LAN_CODE:=}" "${JUMP_LAN_LABEL:=}"
    JUMP_CODE=$(jump_code "$JUMP_CODE" "$PUCK_NAME")
    : "${JUMP_LABEL:=$PUCK_NAME}"
    JUMP_LAN_CODE=$(lan_code "$JUMP_LAN_CODE" "$JUMP_LAN" "$JUMP_CODE")
    [ -z "$JUMP_LAN_CODE" ] || : "${JUMP_LAN_LABEL:=$JUMP_LABEL (LAN)}"
    # shellcheck source=ids.sh
    . "$HERE/ids.sh"
    link_ids
    # The puck's user: the part of SSH_LAN before an @ (none: ssh's default).
    case "$SSH_LAN" in *@*) PUCK_USER="${SSH_LAN%%@*}" ;; *) PUCK_USER="" ;; esac
    case "$TPK_ROUTING" in
        wgt) wgt_vps_settings ;;
        tpk) [ -z "$WGT_SERVER" ] || die "WGT_SERVER is for TPK_ROUTING=wgt; with tpk, empty it and set VPS_SSH and VPS_HOST" ;;
        *) die "TPK_ROUTING must be tpk or wgt" ;;
    esac
    # Sent to the scripts when a step sets them; empty otherwise.
    PUCK_PUBKEY="" VPS_PUBKEY="" CF_TOKEN="" WIFI_SSID="" WIFI_PSK="" WIFI_LIST=""
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
# lan_code <JUMP_LAN_CODE> <JUMP_LAN> <jump code>: the LAN jump's code, if there's
# one: JUMP_LAN_CODE, else with JUMP_LAN=yes the jump code with -l (mom: mom-l).
lan_code() {
    if [ -n "$1" ]; then echo "$1"
    elif [ "$2" = yes ]; then echo "$(shorten "$3" 4)-l"; fi
}

# wgt_vps_settings: TPK_ROUTING=wgt drives the VPS through wgt-ctl: the VPS is
# WGT_SERVER (asked for, if empty), and VPS_SSH (and VPS_HOST, unless set) come
# from wgt-ctl's list, read by its own code.
wgt_vps_settings() {
    [ -x "$WGT_CTL" ] || die "TPK_ROUTING=wgt needs wgt-ctl at $WGT_CTL (tunnel-puck inside a wireguard-tools checkout)"
    [ -n "$WGT_SERVER" ] || choose_wgt_server
    VPS_SSH=$(PYTHONPATH="$REPO" python3 -c 'import sys; from wgt.ctl import remote; print(remote.find(sys.argv[1]).target)' \
        "$WGT_SERVER" 2>/dev/null) || die "WGT_SERVER '$WGT_SERVER' isn't in wgt-ctl's list (see: bin/wgt-ctl list)"
    ! placeholder "$VPS_HOST" || VPS_HOST="${VPS_SSH#*@}"
}

wgt_servers() {    # wgt-ctl's servers, one name per line
    PYTHONPATH="$REPO" python3 -c 'from wgt.ctl import remote
for s in remote.servers(): print(s.name)'
}

# choose_wgt_server: WGT_SERVER, when it's empty: a server from wgt-ctl's list, or a
# new one added with wgt-ctl init (the default when there are none). Saved in the
# puck's settings. Needs a terminal.
choose_wgt_server() {
    have_tty || die "TPK_ROUTING=wgt goes through wgt-ctl: set WGT_SERVER to the VPS's name in bin/wgt-ctl list (not there yet? bin/wgt-ctl init adds it)"
    before=$(wgt_servers) || die "can't read wgt-ctl's server list (bin/wgt-ctl list says why)"
    printf 'TPK_ROUTING=wgt: which wgt server is the VPS for %s?\n' "$PUCK_NAME" >&2
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

# The settings puck.sh and vps.sh read, as sh assignments (sent ahead of ids.sh and the script).
vars() {
    for v in PUCK_NAME TPK_LINK VPS_HOST PUCK_PORT TPK_ROUTING WG_IF JUMP_CODE JUMP_LAN_CODE CLIENT_IPS \
             CLIENT_IP6S WG_SUBNET SSH_CLOUDFLARE PUCK_PUBKEY VPS_PUBKEY CF_TOKEN WIFI_SSID WIFI_PSK WIFI_LIST; do
        eval "val=\${$v}"
        printf '%s=%s\n' "$v" "$(q "$val")"
    done
    [ -z "${TPK_RENEW:-}" ] || echo "TPK_RENEW=$(q "$TPK_RENEW")"
}


# --- Reaching the puck and the VPS -----------------------------------------------

# PUCK (an ssh target), PUCK_ROUTE (lan, link or cloudflare), PUCK_HOP (the
# VPS, an ssh -J hop, for the link) and PUCK_VIA (said in the log): set once per
# run by pick_puck.
PUCK="" PUCK_ROUTE="" PUCK_HOP="" PUCK_VIA=""

probe() { ssh -o BatchMode=yes -o ConnectTimeout=8 "$@" true 2>/dev/null; }
link_target() { if [ -n "$PUCK_USER" ]; then echo "$PUCK_USER@$PUCK_LINK"; else echo "$PUCK_LINK"; fi; }
# Through the link, the puck's host key is kept under its name, in tunnel-puck's
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

# pick_puck: how to reach the puck, the first way that answers.
pick_puck() {
    [ -z "$PUCK" ] || return 0
    require SSH_LAN
    if [ -n "$DRY" ]; then PUCK="$SSH_LAN" PUCK_ROUTE=lan PUCK_VIA="SSH_LAN (dry run: not tried)"
    elif probe "$SSH_LAN"; then
        PUCK="$SSH_LAN" PUCK_ROUTE=lan PUCK_VIA="SSH_LAN"
    elif [ -z "$PUCK_LINK" ] && [ -z "$SSH_CLOUDFLARE" ]; then
        die "can't reach the puck: SSH_LAN ($SSH_LAN) didn't answer, and there's no link yet"
    elif [ -n "$PUCK_LINK" ] && { [ -z "$SSH_CLOUDFLARE" ] \
            || probe $(link_opts "$VPS_SSH") "$(link_target)"; }; then
        PUCK="$(link_target)" PUCK_ROUTE=link PUCK_HOP="$VPS_SSH" PUCK_VIA="the link, through $VPS_SSH"
    else
        PUCK="$SSH_CLOUDFLARE" PUCK_ROUTE=cloudflare PUCK_VIA="SSH_CLOUDFLARE: Cloudflare, the emergency door (SSH_LAN and the link didn't answer)"
    fi
    log "puck: $PUCK, via $PUCK_VIA"
}

# to puck|vps <ssh args...>: ssh to that machine (the VPS through wgt-ctl, with wgt).
to() {
    who="$1"; shift
    if [ "$who" = puck ]; then puck_ssh ssh "$@"
    elif [ -n "$WGT_SERVER" ]; then "$WGT_CTL" "$WGT_SERVER" ssh "$@"
    else ssh "$VPS_SSH" "$@"; fi
}

# run puck|vps <script> <args...>: send the settings, ids.sh and the script; run it as root.
run() {
    who="$1" script="$2"; shift 2
    if [ -n "$DRY" ]; then
        if [ "$who" = puck ]; then log "[dry-run] on $PUCK: $script $*"; else log "[dry-run] on $VPS_SSH: $script $*"; fi
        return 0
    fi
    { vars; cat "$HERE/ids.sh" "$HERE/$script"; } | to "$who" \
        "if [ \"\$(id -u)\" = 0 ]; then sh -s -- $*; else sudo sh -s -- $*; fi"
}

# wgt_vps <wgt args...>: a wgt command on the VPS through wgt-ctl, which also
# delivers changed client configs to ~/wg-tunnels and keeps each device's
# fleet-wide number. stdin is passed through.
wgt_vps() {
    if [ -n "$DRY" ]; then log "[dry-run] bin/wgt-ctl $WGT_SERVER $*"; return 0; fi
    "$WGT_CTL" "$WGT_SERVER" "$@"
}

# confirm_vps <what>: ask once per run before changing the VPS (TPK_YES=1: yes).
confirm_vps() {
    [ -z "$DRY" ] || return 0
    [ "${TPK_YES:-}" != 1 ] || return 0
    [ -z "${CONFIRMED:-}" ] || return 0
    ask_yes "This changes the VPS $VPS_SSH ($1). Continue?" || die "stopped; nothing more was changed on the VPS"
    CONFIRMED=1
}

# Install the exit's rules script on the puck (every routing uses it).
install_exit_script() {
    if [ -n "$DRY" ]; then log "[dry-run] on $PUCK: install tunnel-puck-exit"; return 0; fi
    to puck "$AS_ROOT install -m 755 /dev/stdin /usr/local/sbin/tunnel-puck-exit" < "$HERE/tunnel-puck-exit"
}


# --- Commands ------------------------------------------------------------------

cmd_config() {
    printf '%s\n' "settings: $CONF"
    for v in SSH_LAN SSH_CLOUDFLARE CF_TOKEN_FILE TPK_ROUTING PUCK_PORT TPK_LINK VPS_SSH VPS_HOST \
             CLIENT_IPS CLIENT_IP6S WG_IF WG_SUBNET WGT_SERVER JUMP_CODE JUMP_LABEL JUMP_LAN JUMP_LAN_CODE JUMP_LAN_LABEL; do
        eval "val=\${$v}"
        printf '  %-15s %s\n' "$v" "${val:-(empty)}"
    done
    require SSH_LAN VPS_SSH VPS_HOST
    case "$PUCK_PORT" in *[!0-9]*) die "PUCK_PORT must be a number, or empty (link picks one)" ;; esac
    if [ "$TPK_ROUTING" = wgt ]; then
        for code in $JUMP_CODE $JUMP_LAN_CODE; do    # wgt's own rule for a new jump's code
            PYTHONPATH="$REPO" python3 -c 'import sys; from wgt import store; sys.exit(not store.new_exit_code(sys.argv[1]))' "$code" \
                || die "jump code '$code' won't do: $(PYTHONPATH="$REPO" python3 -c 'from wgt import store; print(store.EXIT_CODE_RULE)'). Set JUMP_CODE (or JUMP_LAN_CODE) yourself"
        done
    elif [ -z "$CLIENT_IPS" ]; then
        log "note: CLIENT_IPS is empty, so no client goes through the puck yet (README 2.2)"
    fi
    if [ -n "$SSH_CLOUDFLARE" ]; then
        require SSH_CLOUDFLARE CF_TOKEN_FILE
        [ -r "$CF_TOKEN_FILE" ] || log "note: CF_TOKEN_FILE $CF_TOKEN_FILE isn't readable yet (tpk-ctl.sh base needs it: step 1.3)"
    else
        log "note: no SSH_CLOUDFLARE, so no emergency door: if the link is down after drop-off, the puck must be power-cycled"
    fi
    check_others
    log "settings look complete"
}

# check_others: refuse what another puck on the same VPS already uses: TPK_LINK,
# PUCK_PORT, and by routing a jump code (wgt) or a client address (tpk). The VPS
# checks the link's subnet and port again when link runs.
check_others() {
    for o in $(pucks); do
        f="$CONFIGS/$o.conf"
        [ "$f" != "$CONF" ] || continue
        # Each value read in a subshell, so the other puck's settings don't leak into these.
        # shellcheck disable=SC1090
        theirs() { ( TPK_ROUTING=tpk TPK_LINK="" PUCK_PORT="" WGT_SERVER="" VPS_SSH="" JUMP_CODE="" JUMP_LAN="" JUMP_LAN_CODE="" CLIENT_IPS=""
                     . "$f"; eval "echo \"$1\"" ); }
        if [ -n "$WGT_SERVER" ]; then [ "$(theirs '$WGT_SERVER')" = "$WGT_SERVER" ] || continue
        else [ "$(theirs '$VPS_SSH')" = "$VPS_SSH" ] || continue; fi
        [ -z "$TPK_LINK" ] || [ "$(theirs '$TPK_LINK')" != "$TPK_LINK" ] \
            || die "puck '$o' on the same VPS also has TPK_LINK $TPK_LINK: empty it, and link picks a free one"
        [ -z "$PUCK_PORT" ] || [ "$(theirs '$PUCK_PORT')" != "$PUCK_PORT" ] \
            || die "puck '$o' on the same VPS also uses PUCK_PORT $PUCK_PORT: empty it, and link picks a free one"
        [ "$(theirs '$TPK_ROUTING')" = "$TPK_ROUTING" ] || continue
        if [ "$TPK_ROUTING" = wgt ]; then mine="$JUMP_CODE $JUMP_LAN_CODE"
            ocode=$(jump_code "$(theirs '$JUMP_CODE')" "$o")
            other="$ocode $(lan_code "$(theirs '$JUMP_LAN_CODE')" "$(theirs '$JUMP_LAN')" "$ocode")"
        else mine="$CLIENT_IPS" other="$(theirs '$CLIENT_IPS')"; fi
        for x in $mine; do
            for y in $other; do
                [ "$x" != "$y" ] || die "puck '$o' on the same VPS also uses $x: a jump code or client goes to one puck (set JUMP_CODE or JUMP_LAN_CODE)"
            done
        done
    done
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
        door=$( (SSH_CLOUDFLARE=""; . "$CONFIGS/$o.conf"; printf '%s' "$SSH_CLOUDFLARE") )
        [ -n "$door" ] || continue    # no emergency door for this puck
        # shellcheck disable=SC1090
        user=$( (SSH_LAN=""; . "$CONFIGS/$o.conf"; [ "${SSH_LAN#*@}" = "$SSH_LAN" ] || printf '%s' "${SSH_LAN%%@*}") )
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

cmd_base() {
    if [ -n "$SSH_CLOUDFLARE" ]; then
        [ -r "$CF_TOKEN_FILE" ] || die "SSH_CLOUDFLARE is set but CF_TOKEN_FILE ($CF_TOKEN_FILE) isn't readable: do step 1.3 first, or empty SSH_CLOUDFLARE"
        CF_TOKEN=$(tr -d ' \n\r' < "$CF_TOKEN_FILE")
        [ -n "$CF_TOKEN" ] || die "CF_TOKEN_FILE ($CF_TOKEN_FILE) is empty"
    fi
    pick_puck
    run puck puck.sh base
    CF_TOKEN=""
}

cmd_link() {
    cmd_config >/dev/null
    install_exit_script
    log "puck: WireGuard and its key"
    PUCK_PUBKEY=$(run puck puck.sh link | sed -n 's/^PUBKEY=//p' | tail -n 1)
    [ -n "$PUCK_PUBKEY" ] || [ -n "$DRY" ] || die "the puck didn't report its public key"
    routing=""; [ "$TPK_ROUTING" != tpk ] || [ -z "$CLIENT_IPS" ] || routing=", routing and a kill switch for $CLIENT_IPS"
    confirm_vps "the link $LINK_IF$routing"
    log "VPS: the link $LINK_IF"
    out=$(run vps vps.sh link)
    VPS_PUBKEY=$(printf '%s\n' "$out" | sed -n 's/^PUBKEY=//p' | tail -n 1)
    [ -n "$VPS_PUBKEY" ] || [ -n "$DRY" ] || die "the VPS didn't report its public key"
    [ -n "$VPS_PUBKEY" ] || VPS_PUBKEY=dry-run
    # What the VPS picked for an empty TPK_LINK or PUCK_PORT is kept in the settings for good.
    picked=$(printf '%s\n' "$out" | sed -n 's/^LINK=//p' | tail -n 1)
    if [ -n "$picked" ] && [ "$picked" != "$TPK_LINK" ]; then TPK_LINK="$picked"; save TPK_LINK "$TPK_LINK"; link_ids; fi
    picked=$(printf '%s\n' "$out" | sed -n 's/^PORT=//p' | tail -n 1)
    if [ -n "$picked" ] && [ "$picked" != "$PUCK_PORT" ]; then PUCK_PORT="$picked"; save PUCK_PORT "$PUCK_PORT"; fi
    if [ -n "$DRY" ] && [ -z "$TPK_LINK" ]; then
        TPK_LINK="10.99.0.0/30"; link_ids; log "[dry-run] the VPS would pick TPK_LINK (and PUCK_PORT if empty)"
    fi
    log "puck: the link"
    run puck puck.sh link >/dev/null
    forget_link_key    # a rebuilt link may lead to a reflashed puck: learn its key afresh
    log "waiting for the first handshake"
    [ -n "$DRY" ] || sleep 8
    run puck puck.sh test || log "the link isn't fully up yet: run tpk-ctl.sh test in a minute"
}

# jump_owner <code> <port>: is the VPS's jump <code> this puck's? Prints "none"
# (no such jump), "ours" (it points at this puck's link, port <port>), or the
# endpoint it points at instead.
jump_owner() {
    ep=$(wgt_vps jump list 2>/dev/null | awk -v c="$1" '$1 == c {print $NF; exit}')
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
        || die "the VPS already has a jump '$1', pointing at $owner, not this puck: set JUMP_CODE (or JUMP_LAN_CODE) to another code, or TPK_TAKEOVER=1 to take it over"
    log "jump '$1' points at $owner; taking it over (TPK_TAKEOVER=1)"
}

jump_port() { if [ "$1" = lan ]; then echo 51823; else echo 51822; fi; }    # the jump server's port on the puck (puck.sh)

# add_jump <isolated|lan> <code> <label>: the puck's jump server, added to wgt.
add_jump() {
    guard_jump "$2" "$(jump_port "$1")"
    out=$(run puck puck.sh jump "$1")
    file=$(printf '%s\n' "$out" | sed -n 's/^JUMPFILE=//p')
    if [ -n "$file" ]; then
        confirm_vps "wgt jump add $2"
        log "VPS: wgt jump add $2 (the jump file goes puck -> VPS through a pipe, never shown)"
        if [ -n "$DRY" ]; then log "[dry-run] would pipe $file into wgt jump add $2"; return 0; fi
        to puck "$AS_ROOT cat $file" | wgt_vps jump add "$2" - --label "$3" --yes \
            || die "wgt jump add $2 failed; the jump file stays on the puck ($file) so tpk-ctl.sh exit can try again with TPK_RENEW=1"
        run puck puck.sh jump-done "$1"
    elif [ -z "$DRY" ]; then
        wgt_vps jump list | grep -qw -- "$2" \
            || die "the puck's jump server exists but the VPS has no jump '$2': run TPK_RENEW=1 tpk-ctl.sh exit"
        log "jump '$2' is already on the VPS"
    fi
}

cmd_exit() {
    cmd_config >/dev/null
    if [ "$TPK_ROUTING" = tpk ]; then
        log "puck: DNS for puck-exit clients"
        run puck puck.sh dns
        return 0
    fi
    install_exit_script
    add_jump isolated "$JUMP_CODE" "$JUMP_LABEL"
    if [ -n "$JUMP_LAN_CODE" ]; then
        require JUMP_LAN_LABEL
        add_jump lan "$JUMP_LAN_CODE" "$JUMP_LAN_LABEL"
    fi
    log "next: tpk-ctl.sh client <device> to give a device a config through the puck"
}

cmd_client() {
    [ "$TPK_ROUTING" = wgt ] \
        || die "tpk-ctl.sh client is for TPK_ROUTING=wgt; with tpk, list the config's address in CLIENT_IPS and run tpk-ctl.sh link"
    device="${1:-}"; [ -n "$device" ] || die "tpk-ctl.sh client <device> [lan]"
    code="$JUMP_CODE"
    if [ "${2:-}" = lan ]; then code="$JUMP_LAN_CODE"; [ -n "$code" ] || die "JUMP_LAN_CODE isn't set"; fi
    confirm_vps "wgt client add $device --exit $code"
    wgt_vps client add "$device" --exit "$code" --warp off --no-qr --yes
    log "made $code.$device; wgt-ctl put it in ~/wg-tunnels/devices/$device/config/ (or: bin/wgt-ctl $WGT_SERVER client show $code.$device --qr)"
}

cmd_lan_mode() {
    mode="${1:-}"
    case "$mode" in isolated|lan) ;; *) die "tpk-ctl.sh lan-mode isolated|lan [<interface>]" ;; esac
    ifc="${2:-}"
    [ -n "$ifc" ] || { if [ "$TPK_ROUTING" = tpk ]; then ifc=wgpk; else ifc=wgrtun; fi; }
    run puck puck.sh lan-mode "$ifc" "$mode"
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

# wifi_list: this puck's Wi-Fi list, configs/<puck>.wifi.conf, checked: "name::password"
# lines (password empty: an open network), most preferred first; # starts a comment.
# Kept mode 600: it holds passwords.
wifi_list() {
    file="$CONFIGS/$PUCK_NAME.wifi.conf" where="$PUCK_NAME.wifi.conf"
    [ -r "$file" ] || die "no Wi-Fi list for $PUCK_NAME: cp configs/puck.wifi.conf.example configs/$where (or tpk-ctl.sh new makes it), and fill it in"
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
    if [ -e "$CONFIGS/$PUCK_NAME.wifi.conf" ]; then log "puck: Wi-Fi from configs/$PUCK_NAME.wifi.conf"; cmd_wifi sync
    else log "no configs/$PUCK_NAME.wifi.conf: the puck's Wi-Fi is left as it is"; fi
}

# test_way_out: with wgt, do clients leave from the puck's network? wgt's jump
# test traces out through the jump without WARP (the path tunnel-puck's configs
# take), and the IP it sees must be the puck's own public one.
test_way_out() {
    what="clients exit from the puck's network"
    if [ "$TPK_ROUTING" != wgt ]; then printf 'SKIP  %s (tested with TPK_ROUTING=wgt only)\n' "$what"; return 0; fi
    puck_ip=$(to puck "curl -4 -fsS -m 10 $TRACE_URL" 2>/dev/null | sed -n 's/^ip=//p')
    out=$(wgt_vps jump test "$JUMP_CODE" 2>&1)
    jump_ip=$(printf '%s\n' "$out" | grep "jump $JUMP_CODE: plain traffic exits through the jump" \
        | sed -n 's/.*(ip=\([^ )]*\).*/\1/p')
    if [ -z "$puck_ip" ]; then
        printf 'FAIL  %s: the puck got no answer from %s\n' "$what" "$TRACE_URL"; return 1
    elif [ -z "$jump_ip" ]; then
        printf 'FAIL  %s: wgt jump test %s gave no plain trace (is wgt on the VPS up to date?):\n' "$what" "$JUMP_CODE"
        printf '%s\n' "$out" | grep -E 'FAIL|ERROR|error|unexpected' | sed 's/^ */      /'
        return 1
    elif [ "$jump_ip" = "$puck_ip" ]; then
        printf 'PASS  %s: %s (wgt jump test %s, without WARP)\n' "$what" "$jump_ip" "$JUMP_CODE"
    else
        printf 'FAIL  clients exit from %s, not the puck'"'"'s network (%s)\n' "$jump_ip" "$puck_ip"; return 1
    fi
}

cmd_test() {
    rc=0
    log "puck ($PUCK)"
    run puck puck.sh test || rc=1
    log "VPS ($VPS_SSH)"
    run vps vps.sh test || rc=1
    if [ -z "$DRY" ]; then
        log "end to end (a client's way out; up to 30s while wgt brings the jump up)"
        test_way_out || rc=1
    fi
    if [ -n "$DRY" ]; then log "(dry run: nothing was tested)"
    elif [ "$rc" = 0 ]; then log "all tests passed"
    else log "some tests failed: see the README's Troubleshooting"; fi
    return "$rc"
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

cmd_undo() {
    what="${1:-}"    # all: cloudflared too (the loop below reuses $1, $2)
    if [ "$PUCK_ROUTE" = link ]; then    # undo removes the link, so it can't run through it
        [ -n "$SSH_CLOUDFLARE" ] || die "tpk-ctl.sh undo can't run through the link it removes: run it at home (SSH_LAN)"
        PUCK="$SSH_CLOUDFLARE" PUCK_ROUTE=cloudflare PUCK_HOP=""; log "puck: $PUCK instead (undo removes the link)"
    fi
    killswitch=""; [ "$TPK_ROUTING" != tpk ] || killswitch=" and its kill switch"
    confirm_vps "remove the link $LINK_IF$killswitch"
    if [ "$TPK_ROUTING" = wgt ]; then
        log "VPS: removing the wgt jump(s). wgt refuses while configs use them: remove those first (bin/wgt-ctl $WGT_SERVER client remove <code>.<device>)"
        set -- "$JUMP_CODE isolated" "$JUMP_LAN_CODE lan"
        for pair in "$@"; do
            set -- $pair; [ $# -eq 2 ] || continue    # no LAN jump
            case "$(jump_owner "$1" "$(jump_port "$2")")" in
                none) continue ;;    # already gone
                ours) wgt_vps jump remove "$1" --yes || die "wgt jump remove $1 failed; nothing else was removed" ;;
                *) log "leaving jump '$1': it isn't this puck's" ;;
            esac
        done
    fi
    run vps vps.sh undo
    run puck puck.sh undo "$what"
}


# --- Main ----------------------------------------------------------------------

main() {
    case "${1:-}" in ''|-h|--help|help) usage; exit 0 ;; esac
    if [ "$1" = new ]; then shift; cmd_new "$@"; exit 0; fi
    if [ "$1" = ssh-config ] && [ "${2:-}" = install ]; then cmd_ssh_install; exit 0; fi

    # Which puck: TPK_CONF (a file), else a first word that isn't a command, else ask.
    if [ -n "${TPK_CONF:-}" ]; then
        CONF="$TPK_CONF"
    elif ! is_command "$1"; then
        CONF="$CONFIGS/$1.conf"
        [ -r "$CONF" ] || die "no puck '$1': configs/$1.conf doesn't exist (configured: $(pucks | tr '\n' ' '))"
        shift
    else
        ask_puck "$1"
        CONF="$CONFIGS/$PICK.conf"
    fi
    DRY="${TPK_DRY_RUN:-}"
    load_settings

    cmd="${1:-}"; [ $# -eq 0 ] || shift
    case "$cmd" in
        config) cmd_config ;;
        ssh-config) cmd_ssh_config ;;
        client) cmd_client "$@" ;;
        base) cmd_base ;;
        link) pick_puck; cmd_link ;;
        exit) pick_puck; cmd_exit ;;
        lan-mode) pick_puck; cmd_lan_mode "$@" ;;
        wifi) pick_puck; cmd_wifi "$@" ;;
        test) pick_puck; cmd_test ;;
        ssh) pick_puck; cmd_ssh "$@" ;;
        reboot|shutdown) pick_puck; cmd_power "$cmd" ;;
        all) pick_puck; cmd_link; cmd_exit; auto_wifi; cmd_test ;;
        undo) pick_puck; cmd_undo "$@" ;;
        *) usage; exit 2 ;;
    esac
}

main "$@"
