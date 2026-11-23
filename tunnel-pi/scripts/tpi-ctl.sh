#!/bin/sh
# tpi-ctl.sh: set up tunnel-pi from your computer (macOS or Linux). Reads ../tunnel-pi.conf (or $TP_CONF)
# and runs pi.sh on the Pi and vps.sh on the VPS over ssh.
#
#   tpi-ctl.sh config              show the settings and check they're filled in
#   tpi-ctl.sh ssh-config          print the ~/.ssh/config entry for SSH_CLOUDFLARE
#   tpi-ctl.sh base                1.3, on the Pi: packages, updates, watchdog (and cloudflared, with SSH_CLOUDFLARE)
#   tpi-ctl.sh link                2.1: the wgpi link (Pi and VPS), keys exchanged for you
#   tpi-ctl.sh exit                2.1, by TPI_ROUTING: tpi's DNS resolver, or the wgt jump(s)
#   tpi-ctl.sh client <device> [lan]   2.2, TPI_ROUTING=wgt: a config for <device> through the Pi (lan: the LAN jump)
#   tpi-ctl.sh lan-mode isolated|lan [<interface>]   2.3: switch LAN mode (default: wgpi or wgjump)
#   tpi-ctl.sh wifi [list]         the Pi's saved Wi-Fi networks, and its uplink now
#   tpi-ctl.sh wifi add <name>     save a Wi-Fi network on the Pi (asks for the password)
#   tpi-ctl.sh wifi remove <name>|all   remove saved Wi-Fi (all: ethernet only)
#   tpi-ctl.sh check               PASS/FAIL on the Pi and the VPS
#   tpi-ctl.sh all                 link, exit, check (after base)
#   tpi-ctl.sh undo [all]          remove it from the VPS and the Pi (all: cloudflared too)
#
# tpi-ctl.sh reaches the Pi the first way that answers: SSH_LAN (at home), then through
# the VPS over the link, then SSH_CLOUDFLARE (Cloudflare, the emergency door for
# when the link is down). Anything that changes the VPS asks first.
# TP_YES=1 skips that question (only once the person has agreed to these changes
# on this VPS). TP_DRY_RUN=1 shows what would run and changes nothing.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
CONF="${TP_CONF:-$HERE/../tunnel-pi.conf}"

log() { printf '==> %s\n' "$*" >&2; }
die() { printf 'tpi-ctl.sh: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,23s/^# \{0,1\}//p' "$0"; }

case "${1:-}" in ''|-h|--help|help) usage; exit 0 ;; esac
[ -r "$CONF" ] || die "no settings at $CONF: cp tunnel-pi.conf.example tunnel-pi.conf, fill it in"
# shellcheck disable=SC1090
. "$CONF"
: "${SSH_LAN:=}" "${SSH_CLOUDFLARE:=}" "${CF_TOKEN_FILE:=}" "${WGT_SERVER:=}" "${VPS_SSH:=}"
: "${VPS_HOST:=}" "${PI_PORT:=}" "${TPI_ROUTING:=tpi}" "${JUMP_CODE:=}" "${JUMP_LABEL:=}"
: "${WG_IF:=wg0}" "${JUMP_LAN_CODE:=}" "${JUMP_LAN_LABEL:=}" "${CLIENT_IPS:=}" "${CLIENT_IP6S:=}" "${WG_SUBNET:=}"
PI_PUBKEY="" VPS_PUBKEY="" CF_TOKEN="" WIFI_SSID="" WIFI_PSK=""
DRY="${TP_DRY_RUN:-}"

placeholder() { case "$1" in ''|*example.com*|*'<'*) return 0 ;; esac; return 1; }

# WGT_SERVER: a server in wgt-ctl's list (local/servers of this checkout). Then the
# VPS is reached through wgt-ctl, and VPS_SSH (and VPS_HOST, unless set) come from
# that list, read by wgt-ctl's own code.
REPO=$(cd "$HERE/../.." && pwd)
WGT_CTL="$REPO/bin/wgt-ctl"
if [ -n "$WGT_SERVER" ]; then
    [ -x "$WGT_CTL" ] || die "WGT_SERVER needs wgt-ctl at $WGT_CTL (tunnel-pi inside a wireguard-tools checkout)"
    VPS_SSH=$(PYTHONPATH="$REPO" python3 -c 'import sys; from wgt.ctl import remote; print(remote.find(sys.argv[1]).target)' \
        "$WGT_SERVER" 2>/dev/null) || die "WGT_SERVER '$WGT_SERVER' isn't in wgt-ctl's list (see: $WGT_CTL list)"
    ! placeholder "$VPS_HOST" || VPS_HOST="${VPS_SSH#*@}"
fi
# The Pi's user: the part of SSH_LAN before the @ (pi if there is none).
case "$SSH_LAN" in *@*) PI_USER="${SSH_LAN%%@*}" ;; *) PI_USER=pi ;; esac

# q <value>: the value quoted for sh.
q() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

# The settings pi.sh and vps.sh read, as sh assignments (sent ahead of the script).
vars() {
    for v in VPS_HOST PI_PORT TPI_ROUTING WG_IF JUMP_CODE JUMP_LAN_CODE CLIENT_IPS CLIENT_IP6S WG_SUBNET \
             SSH_CLOUDFLARE PI_PUBKEY VPS_PUBKEY CF_TOKEN WIFI_SSID WIFI_PSK; do
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
    elif [ -z "$SSH_CLOUDFLARE" ] || probe -o StrictHostKeyChecking=accept-new -J "$VPS_SSH" "$PI_USER@10.99.0.2"; then
        PI="$PI_USER@10.99.0.2" PI_HOP="$VPS_SSH" PI_VIA="the link, through $VPS_SSH"
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
    { vars; cat "$HERE/$script"; } | to "$who" \
        "if [ \"\$(id -u)\" = 0 ]; then sh -s -- $*; else sudo sh -s -- $*; fi"
}

# The remote prefix that runs a command as root (sudo unless already root).
AS_ROOT='if [ "$(id -u)" = 0 ]; then S=; else S=sudo; fi; $S'

# wgt_vps <wgt args...>: a wgt command on the VPS: through wgt-ctl with WGT_SERVER
# (which also delivers changed client configs to ~/wg-tunnels and keeps each
# device's fleet-wide number), else wgt over ssh. stdin is passed through.
wgt_vps() {
    if [ -n "$DRY" ]; then log "[dry-run] on the VPS: wgt $*"; return 0; fi
    if [ -n "$WGT_SERVER" ]; then
        "$WGT_CTL" "$WGT_SERVER" "$@"
    else
        words=""; for a in "$@"; do words="$words $(q "$a")"; done
        ssh "$VPS_SSH" "$AS_ROOT wgt$words"
    fi
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
    for v in SSH_LAN SSH_CLOUDFLARE CF_TOKEN_FILE WGT_SERVER VPS_SSH VPS_HOST PI_PORT TPI_ROUTING \
             CLIENT_IPS CLIENT_IP6S WG_IF WG_SUBNET JUMP_CODE JUMP_LABEL JUMP_LAN_CODE JUMP_LAN_LABEL; do
        eval "val=\${$v}"
        printf '  %-15s %s\n' "$v" "${val:-(empty)}"
    done
    require SSH_LAN VPS_SSH VPS_HOST PI_PORT
    case "$TPI_ROUTING" in
        wgt)  require JUMP_CODE JUMP_LABEL
              case "$JUMP_CODE" in *[!a-z0-9-]*) die "JUMP_CODE: only a-z, 0-9 and -" ;; esac ;;
        tpi)  require CLIENT_IPS ;;
        *) die "TPI_ROUTING must be tpi or wgt" ;;
    esac
    if [ -n "$SSH_CLOUDFLARE" ]; then
        require SSH_CLOUDFLARE CF_TOKEN_FILE
        [ -r "$CF_TOKEN_FILE" ] || log "note: CF_TOKEN_FILE $CF_TOKEN_FILE isn't readable yet (tpi-ctl.sh base needs it: step 1.2)"
    else
        log "note: no SSH_CLOUDFLARE, so no emergency door: if the link is down after drop-off, the friend power-cycles the Pi"
    fi
    log "settings look complete"
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
        [ -r "$CF_TOKEN_FILE" ] || die "SSH_CLOUDFLARE is set but CF_TOKEN_FILE ($CF_TOKEN_FILE) isn't readable: do step 1.2 first, or empty SSH_CLOUDFLARE"
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
    confirm_vps "a wgpi link$( [ "$TPI_ROUTING" = tpi ] && echo ", routing and a kill switch for $CLIENT_IPS")"
    log "VPS: the wgpi link"
    VPS_PUBKEY=$(run vps vps.sh link | pubkey_from)
    [ -n "$VPS_PUBKEY" ] || [ -n "$DRY" ] || die "the VPS didn't report its public key"
    [ -n "$VPS_PUBKEY" ] || VPS_PUBKEY=dry-run
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
    if [ -n "$WGT_SERVER" ]; then
        log "made $code.$device; wgt-ctl put it in ~/wg-tunnels/devices/$device/config/ (or: $WGT_CTL $WGT_SERVER client show $code.$device --qr)"
    else
        log "made $code.$device. On the VPS: wgt client show $code.$device --qr (or wgt client share $code.$device)"
    fi
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
        *) die "tpi-ctl.sh wifi [list] | add <name> | remove <name>|all" ;;
    esac
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
    all) require SSH_LAN; pick_pi; cmd_link; cmd_exit; cmd_check ;;
    undo) require SSH_LAN; pick_pi; shift; cmd_undo "$@" ;;
    *) usage; exit 2 ;;
esac
