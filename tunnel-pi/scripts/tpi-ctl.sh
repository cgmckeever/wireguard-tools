#!/bin/sh
# tpi-ctl.sh: set up tunnel-pi from your computer (macOS or Linux). Reads the Pi's
# settings from ../configs/<pi>.conf (or $TP_CONF) and runs pi.sh on the Pi and
# vps.sh on the VPS over ssh. Without <pi>: the only one there is, else it asks
# (TPI_PI=<pi> is the default, and the answer when there's no terminal).
#
#   tpi-ctl.sh [<pi>] config              show the settings and check they're filled in
#   tpi-ctl.sh [<pi>] ssh-config          print the ~/.ssh/config entry for SSH_CLOUDFLARE
#   tpi-ctl.sh [<pi>] base                1.4, on the Pi: packages, updates, watchdog (and cloudflared, with SSH_CLOUDFLARE)
#   tpi-ctl.sh [<pi>] link                2.1: the wgpi link (Pi and VPS), keys exchanged for you
#   tpi-ctl.sh [<pi>] exit                2.1, by TPI_ROUTING: tpi's DNS resolver, or the wgt jump(s)
#   tpi-ctl.sh [<pi>] client <device> [lan]   2.2, TPI_ROUTING=wgt: a config for <device> through the Pi (lan: the LAN jump)
#   tpi-ctl.sh [<pi>] lan-mode isolated|lan [<interface>]   2.3: switch LAN mode (default: wgpi or wgjump)
#   tpi-ctl.sh [<pi>] wifi [list]         the Pi's saved Wi-Fi networks, and its uplink now
#   tpi-ctl.sh [<pi>] wifi add <name>     save a Wi-Fi network on the Pi (asks for the password)
#   tpi-ctl.sh [<pi>] wifi remove <name>|all   remove saved Wi-Fi (all: ethernet only)
#   tpi-ctl.sh [<pi>] wifi sync           the Pi's Wi-Fi becomes configs/<pi>.wifi.conf, in its order
#   tpi-ctl.sh [<pi>] check               PASS/FAIL on the Pi and the VPS
#   tpi-ctl.sh [<pi>] ssh [<command>]     a shell (or a command) on the Pi, the first way that answers
#   tpi-ctl.sh [<pi>] all                 link, exit, wifi sync (with a <pi>.wifi.conf), check (after base)
#   tpi-ctl.sh [<pi>] undo [all]          remove it from the VPS and the Pi (all: cloudflared too)
#
# tpi-ctl.sh reaches the Pi the first way that answers: SSH_LAN (at home), then through
# the VPS over the link, then SSH_CLOUDFLARE (Cloudflare, the emergency door for
# when the link is down). Anything that changes the VPS asks first.
# TP_YES=1 skips that question (only once the person has agreed to these changes
# on this VPS). TP_DRY_RUN=1 shows what would run and changes nothing.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
CONFIGS=$(cd "$HERE/../configs" && pwd)

log() { printf '==> %s\n' "$*" >&2; }
die() { printf 'tpi-ctl.sh: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,28s/^# \{0,1\}//p' "$0"; printf '\nconfigured Pis: %s\n' "$(pis | tr '\n' ' ' | grep . || echo 'none yet (cp configs/pi.conf.example configs/<name>.conf)')"; }

COMMANDS="config ssh-config base link exit client lan-mode wifi check ssh all undo"
is_command() { case " $COMMANDS " in *" $1 "*) return 0 ;; esac; return 1; }
# pis: the configured Pis, one name per line (configs/<name>.conf).
pis() {
    for f in "$CONFIGS"/*.conf; do
        [ -e "$f" ] || continue
        n=$(basename "$f" .conf)
        case "$n" in *.*) continue ;; esac    # <pi>.wifi.conf: a Pi's Wi-Fi list, not a Pi
        echo "$n"
    done
    return 0
}

case "${1:-}" in ''|-h|--help|help) usage; exit 0 ;; esac
# ask_pi <command>: which Pi, when the command line doesn't say. One: that one.
# Several: a numbered list to choose from, TPI_PI (a name) the default for Enter;
# without a terminal, TPI_PI itself, else stop.
ask_pi() {
    names=$(pis); count=$(printf '%s\n' "$names" | grep -c .)
    default="${TPI_PI:-}"
    if [ -n "$default" ] && ! printf '%s\n' "$names" | grep -qx -- "$default"; then
        die "TPI_PI=$default, but there's no configs/$default.conf (configured: $(echo $names))"
    fi
    case "$count" in
        0) die "no settings yet: cp configs/pi.conf.example configs/<name>.conf, and fill it in" ;;
        1) PICK="$names"; return 0 ;;
    esac
    if ! (: < /dev/tty) 2>/dev/null; then    # no terminal to ask on
        [ -n "$default" ] || die "which Pi? tpi-ctl.sh <pi> $1, or TPI_PI=<pi> (configured: $(echo $names))"
        PICK="$default"; return 0
    fi
    printf 'Which Pi?\n' >&2
    i=0
    for n in $names; do
        i=$((i + 1))
        if [ "$n" = "$default" ]; then printf '  %d) %s  (default)\n' "$i" "$n" >&2; else printf '  %d) %s\n' "$i" "$n" >&2; fi
    done
    printf '[number or name%s]: ' "${default:+, Enter for $default}" >&2
    read -r answer < /dev/tty || answer=""
    [ -n "$answer" ] || answer="$default"
    case "$answer" in
        '') die "no Pi chosen" ;;
        *[!0-9]*) PICK="$answer" ;;
        *) PICK=$(printf '%s\n' "$names" | sed -n "${answer}p") ;;
    esac
    printf '%s\n' "$names" | grep -qx -- "$PICK" || die "no Pi '$answer' (configured: $(echo $names))"
}

# Which Pi: TP_CONF (a file), else a first word that isn't a command, else ask_pi.
if [ -n "${TP_CONF:-}" ]; then
    CONF="$TP_CONF"
elif ! is_command "$1"; then
    CONF="$CONFIGS/$1.conf"
    [ -r "$CONF" ] || die "no Pi '$1': configs/$1.conf doesn't exist (configured: $(pis | tr '\n' ' '))"
    shift
else
    ask_pi "$1"
    CONF="$CONFIGS/$PICK.conf"
fi
PI_NAME=$(basename "$CONF" .conf)
if [ "$CONF" = "$CONFIGS/$PI_NAME.conf" ]; then log "Pi: $PI_NAME (configs/$PI_NAME.conf)"; else log "Pi: $PI_NAME ($CONF)"; fi
# shellcheck disable=SC1090
. "$CONF"
: "${TPI_LINK:=}" "${PI_PORT:=}"
# shellcheck source=ids.sh
. "$HERE/ids.sh"
link_ids
: "${SSH_LAN:=}" "${SSH_CLOUDFLARE:=}" "${CF_TOKEN_FILE:=}" "${WGT_SERVER:=}" "${VPS_SSH:=}"
: "${VPS_HOST:=}" "${PI_PORT:=}" "${TPI_ROUTING:=tpi}" "${JUMP_CODE:=}" "${JUMP_LABEL:=}"
# jump_default <file's JUMP_CODE> <Pi name>: the jump code, the Pi's name when it's empty.
jump_default() { if [ -n "$1" ]; then echo "$1"; else echo "$2"; fi; }
JUMP_CODE=$(jump_default "$JUMP_CODE" "$PI_NAME")
: "${JUMP_LABEL:=$PI_NAME}"
: "${WG_IF:=wg0}" "${JUMP_LAN_CODE:=}" "${JUMP_LAN_LABEL:=}" "${CLIENT_IPS:=}" "${CLIENT_IP6S:=}" "${WG_SUBNET:=}"
PI_PUBKEY="" VPS_PUBKEY="" CF_TOKEN="" WIFI_SSID="" WIFI_PSK="" WIFI_LIST=""
DRY="${TP_DRY_RUN:-}"

placeholder() { case "$1" in ''|*example.com*|*'<'*) return 0 ;; esac; return 1; }

# save <NAME> <value>: set NAME="value" in this Pi's settings file (replacing its
# line, or adding one), and say so.
save() {
    tmp=$(mktemp)
    awk -v k="$1" -v line="$1=\"$2\"" 'index($0, k "=") == 1 && !done {print line; done = 1; next} {print}
        END {if (!done) print line}' "$CONF" > "$tmp"
    cat "$tmp" > "$CONF"; rm -f "$tmp"
    log "saved $1=\"$2\" in $CONF"
}

# wgt_servers: the servers in wgt-ctl's list, one name per line (read by its own code).
wgt_servers() {
    PYTHONPATH="$REPO" python3 -c 'from wgt.ctl import remote
for s in remote.servers(): print(s.name)'
}

# choose_wgt_server: WGT_SERVER, when it's empty: a server from wgt-ctl's list, or a
# new one added with wgt-ctl init. Saved in the Pi's settings. Needs a terminal.
choose_wgt_server() {
    (: < /dev/tty) 2>/dev/null \
        || die "TPI_ROUTING=wgt goes through wgt-ctl: set WGT_SERVER to the VPS's name in bin/wgt-ctl list (not there yet? bin/wgt-ctl init adds it)"
    before=$(wgt_servers) || die "can't read wgt-ctl's server list (bin/wgt-ctl list says why)"
    printf 'TPI_ROUTING=wgt: which wgt server is the VPS for %s?\n' "$PI_NAME" >&2
    i=0
    for n in $before; do i=$((i + 1)); printf '  %d) %s\n' "$i" "$n" >&2; done
    printf '  a) add a new one: bin/wgt-ctl init sets up wgt on it (or adopts one already running it)\n' >&2
    if [ -z "$before" ]; then printf '[a]: ' >&2; else printf '[number or a]: ' >&2; fi
    read -r answer < /dev/tty || answer=""
    [ -n "$answer" ] || [ -n "$before" ] || answer=a
    case "$answer" in
        a|A)
            [ -z "$DRY" ] || die "a dry run doesn't add a server: run it without TP_DRY_RUN, or set WGT_SERVER"
            printf 'wgt-ctl init connects to that server and sets it up (or adopts it). Continue? [y/N] ' >&2
            read -r yes < /dev/tty || yes=""
            case "$yes" in y|Y|yes) ;; *) die "stopped; nothing was changed" ;; esac
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

# TPI_ROUTING=wgt: the VPS is WGT_SERVER, a server in wgt-ctl's list (local/servers
# of this checkout), and everything on it goes through wgt-ctl. VPS_SSH (and
# VPS_HOST, unless set) come from that list, read by wgt-ctl's own code.
# TPI_ROUTING=tpi: VPS_SSH and VPS_HOST, over plain ssh.
REPO=$(cd "$HERE/../.." && pwd)
WGT_CTL="$REPO/bin/wgt-ctl"
if [ "$TPI_ROUTING" = wgt ]; then
    [ -x "$WGT_CTL" ] || die "TPI_ROUTING=wgt needs wgt-ctl at $WGT_CTL (tunnel-pi inside a wireguard-tools checkout)"
    [ -n "$WGT_SERVER" ] || choose_wgt_server
    VPS_SSH=$(PYTHONPATH="$REPO" python3 -c 'import sys; from wgt.ctl import remote; print(remote.find(sys.argv[1]).target)' \
        "$WGT_SERVER" 2>/dev/null) || die "WGT_SERVER '$WGT_SERVER' isn't in wgt-ctl's list (see: bin/wgt-ctl list)"
    ! placeholder "$VPS_HOST" || VPS_HOST="${VPS_SSH#*@}"
elif [ -n "$WGT_SERVER" ]; then
    die "WGT_SERVER is for TPI_ROUTING=wgt; with tpi, empty it and set VPS_SSH and VPS_HOST"
fi
# The Pi's user: the part of SSH_LAN before the @ (pi if there is none).
case "$SSH_LAN" in *@*) PI_USER="${SSH_LAN%%@*}" ;; *) PI_USER=pi ;; esac

# q <value>: the value quoted for sh.
q() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

# The settings pi.sh and vps.sh read, as sh assignments (sent ahead of the script).
vars() {
    for v in PI_NAME TPI_LINK VPS_HOST PI_PORT TPI_ROUTING WG_IF JUMP_CODE JUMP_LAN_CODE CLIENT_IPS CLIENT_IP6S WG_SUBNET \
             SSH_CLOUDFLARE PI_PUBKEY VPS_PUBKEY CF_TOKEN WIFI_SSID WIFI_PSK WIFI_LIST; do
        eval "val=\${$v}"
        printf '%s=%s\n' "$v" "$(q "$val")"
    done
    [ -z "${TP_RENEW:-}" ] || echo "TP_RENEW=$(q "$TP_RENEW")"
}

# pick_pi: choose how to reach the Pi, once per run. Sets PI (an ssh target),
# PI_VIA (said in the log) and PI_HOP (an ssh -J hop through the VPS, for the link).
PI="" PI_VIA="" PI_HOP=""
probe() { ssh -o BatchMode=yes -o ConnectTimeout=8 "$@" true 2>/dev/null; }
pick_pi() {
    [ -z "$PI" ] || return 0
    if [ -n "$DRY" ]; then PI="$SSH_LAN" PI_VIA="SSH_LAN (dry run: not tried)"
    elif probe "$SSH_LAN"; then
        PI="$SSH_LAN" PI_VIA="SSH_LAN"
    elif [ -z "$PI_LINK" ] && [ -z "$SSH_CLOUDFLARE" ]; then
        die "can't reach the Pi: SSH_LAN ($SSH_LAN) didn't answer, and there's no link yet"
    elif [ -n "$PI_LINK" ] && { [ -z "$SSH_CLOUDFLARE" ] || probe -o StrictHostKeyChecking=accept-new -J "$VPS_SSH" "$PI_USER@$PI_LINK"; }; then
        PI="$PI_USER@$PI_LINK" PI_HOP="$VPS_SSH" PI_VIA="the link, through $VPS_SSH"
    else
        PI="$SSH_CLOUDFLARE" PI_VIA="SSH_CLOUDFLARE: Cloudflare, the emergency door (SSH_LAN and the link didn't answer)"
    fi
    log "Pi: $PI, via $PI_VIA"
}

# to pi|vps <ssh args...>: ssh to that machine.
to() {
    who="$1"; shift
    if [ "$who" = vps ] && [ -n "$WGT_SERVER" ]; then "$WGT_CTL" "$WGT_SERVER" ssh "$@"
    elif [ "$who" = vps ]; then ssh "$VPS_SSH" "$@"
    elif [ -n "$PI_HOP" ]; then ssh -o StrictHostKeyChecking=accept-new -J "$PI_HOP" "$PI" "$@"
    else ssh "$PI" "$@"; fi
}

name() { if [ "$1" = vps ]; then echo "$VPS_SSH"; else echo "$PI"; fi; }

# run pi|vps <script> <args...>: stream the settings and the script; run it as root.
run() {
    who="$1" script="$2"; shift 2
    if [ -n "$DRY" ]; then log "[dry-run] on $(name "$who"): $script $*"; return 0; fi
    { vars; cat "$HERE/ids.sh" "$HERE/$script"; } | to "$who" \
        "if [ \"\$(id -u)\" = 0 ]; then sh -s -- $*; else sudo sh -s -- $*; fi"
}

# The remote prefix that runs a command as root (sudo unless already root).
AS_ROOT='if [ "$(id -u)" = 0 ]; then S=; else S=sudo; fi; $S'

# wgt_vps <wgt args...>: a wgt command on the VPS, through wgt-ctl (which also
# delivers changed client configs to ~/wg-tunnels and keeps each device's
# fleet-wide number). stdin is passed through.
wgt_vps() {
    if [ -n "$DRY" ]; then log "[dry-run] $WGT_CTL $WGT_SERVER $*"; return 0; fi
    "$WGT_CTL" "$WGT_SERVER" "$@"
}

require() {
    for v in "$@"; do
        eval "val=\${$v}"
        ! placeholder "$val" || die "$v in $CONF is empty or still the example"
    done
}

confirm_vps() {
    [ -z "$DRY" ] || return 0
    [ "${TP_YES:-}" = 1 ] && return 0
    [ -n "${CONFIRMED:-}" ] && return 0
    printf 'This changes the VPS %s (%s). Continue? [y/N] ' "$VPS_SSH" "$1" >&2
    read -r answer < /dev/tty || answer=""
    case "$answer" in y|Y|yes) CONFIRMED=1 ;; *) die "stopped; nothing more was changed on the VPS" ;; esac
}

pubkey_from() { sed -n 's/^PUBKEY=//p' | tail -n 1; }

cmd_config() {
    printf '%s\n' "settings: $CONF"
    for v in SSH_LAN SSH_CLOUDFLARE CF_TOKEN_FILE TPI_ROUTING PI_PORT TPI_LINK VPS_SSH VPS_HOST \
             CLIENT_IPS CLIENT_IP6S WG_IF WG_SUBNET WGT_SERVER JUMP_CODE JUMP_LABEL JUMP_LAN_CODE JUMP_LAN_LABEL; do
        eval "val=\${$v}"
        printf '  %-15s %s\n' "$v" "${val:-(empty)}"
    done
    require SSH_LAN VPS_SSH VPS_HOST
    case "$PI_PORT" in *[!0-9]*) die "PI_PORT must be a number, or empty (link picks one)" ;; esac
    case "$TPI_ROUTING" in
        wgt)  for code in $JUMP_CODE $JUMP_LAN_CODE; do    # wgt's own rule for a new jump's code
                  PYTHONPATH="$REPO" python3 -c 'import sys; from wgt import store; sys.exit(not store.new_exit_code(sys.argv[1]))' "$code" \
                      || die "jump code '$code' won't do: $(PYTHONPATH="$REPO" python3 -c 'from wgt import store; print(store.EXIT_CODE_RULE)'). Set JUMP_CODE (empty, it's the Pi's name)"
              done ;;
        tpi)  [ -n "$CLIENT_IPS" ] || log "note: CLIENT_IPS is empty, so no client goes through the Pi yet (README 2.2)" ;;
        *) die "TPI_ROUTING must be tpi or wgt" ;;
    esac
    if [ -n "$SSH_CLOUDFLARE" ]; then
        require SSH_CLOUDFLARE CF_TOKEN_FILE
        [ -r "$CF_TOKEN_FILE" ] || log "note: CF_TOKEN_FILE $CF_TOKEN_FILE isn't readable yet (tpi-ctl.sh base needs it: step 1.3)"
    else
        log "note: no SSH_CLOUDFLARE, so no emergency door: if the link is down after drop-off, the friend power-cycles the Pi"
    fi
    others
    log "settings look complete"
}

# others: refuse what another Pi on the same VPS already uses (TPI_LINK, PI_PORT,
# a jump code, a client). The VPS checks again when link runs.
others() {
    for o in $(pis); do
        f="$CONFIGS/$o.conf"
        [ "$f" != "$CONF" ] || continue
        # shellcheck disable=SC1090,SC2030
        set -- $( (TPI_LINK="" PI_PORT="" WGT_SERVER="" VPS_SSH=""
                  . "$f"; printf '%s %s %s %s' "${TPI_LINK:--}" "${PI_PORT:--}" "${WGT_SERVER:--}" "${VPS_SSH:--}") )
        same_vps=""
        if [ -n "$WGT_SERVER" ] && [ "$3" = "$WGT_SERVER" ]; then same_vps=1; fi
        [ "$4" != "$VPS_SSH" ] || same_vps=1
        [ -n "$same_vps" ] || continue
        [ -z "$TPI_LINK" ] || [ "$1" != "$TPI_LINK" ] || die "Pi '$o' on the same VPS also has TPI_LINK $TPI_LINK: empty it, and link picks a free one"
        [ -z "$PI_PORT" ] || [ "$2" != "$PI_PORT" ] || die "Pi '$o' on the same VPS also uses PI_PORT $PI_PORT: empty it, and link picks a free one"
        # By routing: wgt's jump codes, or tpi's client addresses, each belong to one Pi.
        if [ "$TPI_ROUTING" = wgt ]; then mine="$JUMP_CODE $JUMP_LAN_CODE"
            theirs=$( (TPI_ROUTING=tpi JUMP_CODE="" JUMP_LAN_CODE=""; . "$f"; [ "$TPI_ROUTING" != wgt ] || echo "$(jump_default "$JUMP_CODE" "$o") $JUMP_LAN_CODE") )
        else mine="$CLIENT_IPS"
            theirs=$( (TPI_ROUTING=tpi CLIENT_IPS=""; . "$f"; [ "$TPI_ROUTING" != tpi ] || echo "$CLIENT_IPS") )
        fi
        for x in $mine; do
            for y in $theirs; do
                [ "$x" != "$y" ] || die "Pi '$o' on the same VPS also uses $x: a jump code or client goes to one Pi"
            done
        done
    done
}

cmd_ssh_config() {
    require SSH_CLOUDFLARE
    bin=$(command -v cloudflared || echo cloudflared)
    cat <<EOF
# Add to ~/.ssh/config. cloudflared must be installed on this computer first
# (macOS: brew install cloudflared; Linux: Cloudflare's package for your system).

Host $SSH_CLOUDFLARE
    User $PI_USER
    ProxyCommand $bin access ssh --hostname %h
EOF
}

cmd_base() {
    require SSH_LAN
    if [ -n "$SSH_CLOUDFLARE" ]; then
        [ -r "$CF_TOKEN_FILE" ] || die "SSH_CLOUDFLARE is set but CF_TOKEN_FILE ($CF_TOKEN_FILE) isn't readable: do step 1.3 first, or empty SSH_CLOUDFLARE"
        CF_TOKEN=$(tr -d ' \n\r' < "$CF_TOKEN_FILE")
        [ -n "$CF_TOKEN" ] || die "CF_TOKEN_FILE ($CF_TOKEN_FILE) is empty"
    fi
    pick_pi
    run pi pi.sh base
    CF_TOKEN=""
}

# Install the exit rules script on the Pi (every TPI_ROUTING uses it).
install_exit_script() {
    if [ -n "$DRY" ]; then log "[dry-run] on $PI: install tunnel-pi-exit"; return 0; fi
    to pi "$AS_ROOT install -m 755 /dev/stdin /usr/local/sbin/tunnel-pi-exit" < "$HERE/tunnel-pi-exit"
}


cmd_link() {
    cmd_config >/dev/null
    install_exit_script
    log "Pi: WireGuard and its key"
    PI_PUBKEY=$(run pi pi.sh link | pubkey_from)
    [ -n "$PI_PUBKEY" ] || [ -n "$DRY" ] || die "the Pi didn't report its public key"
    confirm_vps "the link $LINK_IF$( [ "$TPI_ROUTING" = tpi ] && [ -n "$CLIENT_IPS" ] && echo ", routing and a kill switch for $CLIENT_IPS")"
    log "VPS: the link $LINK_IF"
    out=$(run vps vps.sh link)
    VPS_PUBKEY=$(printf '%s\n' "$out" | pubkey_from)
    [ -n "$VPS_PUBKEY" ] || [ -n "$DRY" ] || die "the VPS didn't report its public key"
    [ -n "$VPS_PUBKEY" ] || VPS_PUBKEY=dry-run
    # What the VPS picked for an empty TPI_LINK or PI_PORT: kept in the settings for good.
    picked=$(printf '%s\n' "$out" | sed -n 's/^LINK=//p' | tail -n 1)
    if [ -n "$picked" ] && [ "$picked" != "$TPI_LINK" ]; then TPI_LINK="$picked"; save TPI_LINK "$TPI_LINK"; link_ids; fi
    picked=$(printf '%s\n' "$out" | sed -n 's/^PORT=//p' | tail -n 1)
    if [ -n "$picked" ] && [ "$picked" != "$PI_PORT" ]; then PI_PORT="$picked"; save PI_PORT "$PI_PORT"; fi
    if [ -n "$DRY" ] && [ -z "$TPI_LINK" ]; then TPI_LINK="10.99.0.0/30"; link_ids; log "[dry-run] the VPS would pick TPI_LINK (and PI_PORT if empty)"; fi
    log "Pi: the wgpi link"
    run pi pi.sh link >/dev/null
    log "waiting for the first handshake"
    [ -n "$DRY" ] || sleep 8
    run pi pi.sh check || log "the link isn't fully up yet: run tpi-ctl.sh check in a minute"
}

# add_jump <isolated|lan> <code> <label>
add_jump() {
    out=$(run pi pi.sh jump "$1")
    file=$(printf '%s\n' "$out" | sed -n 's/^JUMPFILE=//p')
    if [ -n "$file" ]; then
        confirm_vps "wgt jump add $2"
        log "VPS: wgt jump add $2 (the jump file goes Pi -> VPS through a pipe, never shown)"
        if [ -n "$DRY" ]; then log "[dry-run] would pipe $file into wgt jump add $2"; return 0; fi
        if to pi "$AS_ROOT cat $file" | wgt_vps jump add "$2" - --label "$3" --yes; then
            run pi pi.sh jump-done "$1"
        else
            die "wgt jump add $2 failed; the jump file stays on the Pi ($file) so tpi-ctl.sh exit can try again with TP_RENEW=1"
        fi
    elif [ -z "$DRY" ]; then
        wgt_vps jump list | grep -qw -- "$2" \
            || die "the Pi's jump server exists but the VPS has no jump '$2': run TP_RENEW=1 tpi-ctl.sh exit"
        log "jump '$2' is already on the VPS"
    fi
}

cmd_exit() {
    cmd_config >/dev/null
    if [ "$TPI_ROUTING" = tpi ]; then
        log "Pi: DNS for Pi-exit clients"
        run pi pi.sh dns
    else
        install_exit_script
        add_jump isolated "$JUMP_CODE" "$JUMP_LABEL"
        if [ -n "$JUMP_LAN_CODE" ]; then
            require JUMP_LAN_LABEL
            add_jump lan "$JUMP_LAN_CODE" "$JUMP_LAN_LABEL"
        fi
        log "next: tpi-ctl.sh client <device> to give a device a config through the Pi"
    fi
}

cmd_client() {
    [ "$TPI_ROUTING" = wgt ] || die "tpi-ctl.sh client is for TPI_ROUTING=wgt; with TPI_ROUTING=tpi, list the config's address in CLIENT_IPS and run tpi-ctl.sh link"
    device="${1:-}"; [ -n "$device" ] || die "tpi-ctl.sh client <device> [lan]"
    code="$JUMP_CODE"
    if [ "${2:-}" = lan ]; then code="$JUMP_LAN_CODE"; [ -n "$code" ] || die "JUMP_LAN_CODE isn't set"; fi
    confirm_vps "wgt client add $device --exit $code"
    wgt_vps client add "$device" --exit "$code" --warp off --no-qr --yes
    log "made $code.$device; wgt-ctl put it in ~/wg-tunnels/devices/$device/config/ (or: $WGT_CTL $WGT_SERVER client show $code.$device --qr)"
}

cmd_lan_mode() {
    mode="${1:-}"
    case "$mode" in isolated|lan) ;; *) die "tpi-ctl.sh lan-mode isolated|lan [<interface>]" ;; esac
    ifc="${2:-}"
    [ -n "$ifc" ] || { if [ "$TPI_ROUTING" = tpi ]; then ifc=wgpi; else ifc=wgjump; fi; }
    run pi pi.sh lan-mode "$ifc" "$mode"
}

# read_password <prompt>: from the terminal without echo, or one line of stdin
# when it isn't a terminal (tpi-ctl.sh wifi add name < file). Never an argument.
read_password() {
    if [ -t 0 ]; then
        printf '%s' "$1" >&2
        stty -echo; IFS= read -r pw || pw=""; stty echo; echo >&2
    else
        IFS= read -r pw || pw=""
    fi
}

# wifi_list: this Pi's Wi-Fi list, configs/<pi>.wifi.conf, checked: "ssid::password"
# lines (password empty: an open network), most preferred first; # starts a comment.
# Kept mode 600: it holds passwords.
wifi_list() {
    file="$CONFIGS/$PI_NAME.wifi.conf"
    [ -r "$file" ] || die "no Wi-Fi list for $PI_NAME: cp configs/pi.wifi.conf.example configs/$PI_NAME.wifi.conf, and fill it in"
    chmod 600 "$file"
    n=0
    while IFS= read -r line || [ -n "$line" ]; do
        n=$((n + 1))
        case "$line" in ''|'#'*) continue ;; *::*) ;; *) die "$PI_NAME.wifi.conf line $n: want name::password (password empty for an open network)" ;; esac
        [ -n "${line%%::*}" ] || die "$PI_NAME.wifi.conf line $n: the network name is empty"
        psk="${line#*::}"
        case "${#psk}" in 0|[89]|[1-5][0-9]|6[0-3]) ;; *) die "$PI_NAME.wifi.conf line $n: a Wi-Fi password is 8 to 63 characters" ;; esac
        printf '%s\n' "$line"
    done < "$file"
}

# auto_wifi: in all, sync the Pi's Wi-Fi to its list when there is one; with none,
# the Pi's Wi-Fi stays as it is.
auto_wifi() {
    if [ -e "$CONFIGS/$PI_NAME.wifi.conf" ]; then log "Pi: Wi-Fi from configs/$PI_NAME.wifi.conf"; cmd_wifi sync
    else log "no configs/$PI_NAME.wifi.conf: the Pi's Wi-Fi is left as it is"; fi
}

cmd_wifi() {
    case "${1:-list}" in
        list) install_exit_script; run pi pi.sh wifi-list ;;
        add)
            WIFI_SSID="${2:-}"; [ -n "$WIFI_SSID" ] || die "tpi-ctl.sh wifi add <network name>"
            if [ -z "$DRY" ]; then
                read_password "Password for '$WIFI_SSID' (empty for an open network): "
                case "${#pw}" in 0|[89]|[1-5][0-9]|6[0-3]) ;; *) die "a Wi-Fi password is 8 to 63 characters" ;; esac
                WIFI_PSK="$pw"; pw=""
            fi
            install_exit_script
            run pi pi.sh wifi-add
            WIFI_PSK="" ;;
        remove)
            WIFI_SSID="${2:-}"; [ -n "$WIFI_SSID" ] || die "tpi-ctl.sh wifi remove <network name>|all"
            install_exit_script
            run pi pi.sh wifi-remove ;;
        sync)
            WIFI_LIST=$(wifi_list)
            install_exit_script
            run pi pi.sh wifi-sync
            WIFI_LIST="" ;;
        *) die "tpi-ctl.sh wifi [list] | add <name> | remove <name>|all | sync" ;;
    esac
}

# cmd_ssh [<command>]: a shell on the Pi (or one command), the way pick_pi found.
cmd_ssh() {
    if [ -n "$PI_HOP" ]; then exec ssh -t -o StrictHostKeyChecking=accept-new -J "$PI_HOP" "$PI" "$@"
    else exec ssh -t "$PI" "$@"; fi
}

cmd_check() {
    rc=0
    log "Pi ($PI)"
    run pi pi.sh check || rc=1
    log "VPS ($VPS_SSH)"
    run vps vps.sh check || rc=1
    if [ -n "$DRY" ]; then log "(dry run: nothing was checked)"
    elif [ "$rc" = 0 ]; then log "all checks passed"
    else log "some checks failed: see the README's Troubleshooting"; fi
    return "$rc"
}

cmd_undo() {
    if [ -n "$PI_HOP" ]; then   # it removes the link, so it can't run through it
        [ -n "$SSH_CLOUDFLARE" ] || die "tpi-ctl.sh undo can't run through the link it removes: run it at home (SSH_LAN)"
        PI="$SSH_CLOUDFLARE" PI_HOP=""; log "Pi: $PI instead (undo removes the link)"
    fi
    confirm_vps "remove wgpi$( [ "$TPI_ROUTING" = tpi ] && echo " and the kill switch")"
    if [ "$TPI_ROUTING" = wgt ]; then
        log "VPS: removing the wgt jump(s). wgt refuses while configs use them: remove those first (wgt client remove <code>.<device>)"
        for code in $JUMP_CODE $JUMP_LAN_CODE; do
            wgt_vps jump list | grep -qw -- "$code" || continue   # already gone
            wgt_vps jump remove "$code" --yes || die "wgt jump remove $code failed; nothing else was removed"
        done
    fi
    run vps vps.sh undo
    run pi pi.sh undo "${1:-}"
}

case "${1:-}" in
    config) cmd_config ;;
    ssh-config) cmd_ssh_config ;;
    base) require SSH_LAN; cmd_base ;;
    link) require SSH_LAN; pick_pi; cmd_link ;;
    exit) require SSH_LAN; pick_pi; cmd_exit ;;
    client) shift; cmd_client "$@" ;;
    lan-mode) require SSH_LAN; pick_pi; shift; cmd_lan_mode "$@" ;;
    wifi) require SSH_LAN; pick_pi; shift; cmd_wifi "$@" ;;
    check) require SSH_LAN; pick_pi; cmd_check ;;
    ssh) require SSH_LAN; pick_pi; shift; cmd_ssh "$@" ;;
    all) require SSH_LAN; pick_pi; cmd_link; cmd_exit; auto_wifi; cmd_check ;;
    undo) require SSH_LAN; pick_pi; shift; cmd_undo "$@" ;;
    *) usage; exit 2 ;;
esac
