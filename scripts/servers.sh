#!/usr/bin/env bash
# servers.sh — Manage the wireguard-tools servers this machine looks after,
# from this machine (the remote side of wg-setup.sh / wg-client.sh).
#
# The list lives in local/servers (untracked): "name  ssh-target  [checkout]"
# per line. Private files for each server stay under local/ too:
#   local/exits/<name>/*.conf        Proton configs for its exit cities (any name)
#   local/exits/details.tsv          server, local file, code on the server and
#                                    full name, tab-separated (asked at first push)
#   local/backups/<name>/            backup archives fetched from it
#
# Usage: scripts/servers.sh [command] [server|all] [args...]
#   (no command)          list the servers, with branch and reachability
#   init [--branch B] [--address A] [--name N] [-- setup flags]
#                         set up a new server: install git, clone this repo,
#                         run wg-setup.sh setup --name N, then add it to the
#                         list. A server that already runs wireguard-tools is
#                         adopted (recorded under its saved name) instead.
#   ssh <s>               log in
#   pull <s|all>          git pull its checkout
#   branch <s> <branch>   switch its checkout to <branch> (then pull)
#   setup|warp|status|test|exit|log|rollback <s|all> [args]
#                         run wg-setup.sh <command> [args] there
#                         (log [lines] [-f]: its changes, watchdogs and tunnels)
#   client <s> [args]     run wg-client.sh [args] there (e.g. client boba list)
#   push-exits <s> [file...]
#                         install local/exits/<s>/*.conf as its exit cities
#                         (scp, wg-setup.sh exit add, delete the upload); a new
#                         file is named once: "Paris, France" -> code par-fr
#   backup <s|all>        wg-setup.sh backup there, then fetch the archive to
#                         local/backups/<s>/ (holds private keys; mode 600)
#   forget <s> [--yes]    remove from the list (nothing on the server changes)
#
# <s> is a list number or name; left out, you pick from the list. Runs with
# macOS's bash 3.2.

set -uo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
LOCAL="$ROOT/local"
LIST="$LOCAL/servers"
LINKS="$LOCAL/exits/details.tsv"   # server  local-file  code  full name (tab-separated)
DEF_DIR=/opt/wireguard-tools
SSH_OPTS=(-o ConnectTimeout=10 -o ServerAliveInterval=15)

die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log()  { printf '==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }

ensure_local() {
    mkdir -p "$LOCAL/exits" "$LOCAL/backups"
    chmod 700 "$LOCAL" "$LOCAL/exits" "$LOCAL/backups"
    if [ ! -f "$LIST" ]; then
        printf '%s\n' "# Servers this machine manages (untracked): name  ssh-target  [checkout]" >"$LIST"
    fi
    chmod 600 "$LIST"
}

# "name target dir" per server.
entries() {
    [ -f "$LIST" ] || return 0
    awk -v d="$DEF_DIR" '!/^[[:space:]]*(#|$)/ { print $1, $2, ($3 == "" ? d : $3) }' "$LIST"
}

# Resolve a number or name to "name target dir"; no argument: pick one.
pick() {
    local want="${1:-}" n=0 line reply
    if [ -z "$want" ]; then
        [ -n "$(entries)" ] || die "no servers yet (add one: $0 init)"
        cmd_list quick >&2
        read -r -p "  Which server? [number or name] " reply </dev/tty || true
        want="$reply"
    fi
    [ -n "$want" ] || die "no server chosen"
    while read -r line; do
        n=$(( n + 1 ))
        if [ "$want" = "$n" ] || [ "$want" = "${line%% *}" ]; then echo "$line"; return 0; fi
    done < <(entries)
    die "no server '$want' in $LIST"
}

# Run a command for one server, or every server with "all".
each() {
    local sel="$1" fn="$2" line
    shift 2
    if [ "$sel" = all ]; then
        while read -r line; do
            log "${line%% *}"
            # shellcheck disable=SC2086  # line holds "name target dir"
            "$fn" $line "$@" </dev/null || true
            echo ""
        done < <(entries)
    else
        line=$(pick "$sel") || exit 1
        # shellcheck disable=SC2086
        "$fn" $line "$@"
    fi
}

# ssh with a terminal when we have one (prompts, QR codes, confirmations).
rsh() {
    local target="$1"
    shift
    if [ -t 0 ] && [ -t 1 ]; then ssh -t "${SSH_OPTS[@]}" "$target" "$@"; else ssh "${SSH_OPTS[@]}" "$target" "$@"; fi
}

# Quote arguments for the remote shell.
q() {
    local a out=""
    for a in "$@"; do out="$out $(printf '%q' "$a")"; done
    echo "${out# }"
}

repo_url() {
    local u
    u=$(git -C "$ROOT" remote get-url origin 2>/dev/null) || die "no git remote 'origin' in $ROOT"
    # Servers clone anonymously: turn git@github.com:owner/repo into https.
    if [[ $u =~ ^git@([^:]+):(.+)$ ]]; then u="https://${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"; fi
    echo "$u"
}

# --- list ---

cmd_list() {
    local mode="${1:-}" n=0 name target dir probe staged
    if [ -z "$(entries)" ]; then
        info "no servers yet (add one: $0 init)"
        return 0
    fi
    echo ""
    while read -r name target dir; do
        n=$(( n + 1 ))
        staged=$(cd "$LOCAL/exits/$name" 2>/dev/null && ls -- *.conf 2>/dev/null | sed 's/\.conf$//' | paste -sd, -)
        probe=""
        if [ "$mode" != quick ]; then
            probe=$(ssh -n -o BatchMode=yes -o ConnectTimeout=5 "$target" \
                "git -C $dir rev-parse --abbrev-ref HEAD 2>/dev/null | tr -d '\n'; printf '@'; git -C $dir rev-parse --short HEAD 2>/dev/null" \
                2>/dev/null) || probe="unreachable"
        fi
        printf '  %2d) %-10s %-24s %-22s %s\n' "$n" "$name" "$target" "$probe" "${staged:+exits staged: $staged}"
    done < <(entries)
    echo ""
}

# --- init ---

cmd_init() {
    local address="" name="" user=root branch=main setup_args=() target url probe saved os
    while [ $# -gt 0 ]; do
        case "$1" in
            --address) address="$2"; shift ;;
            --name)    name="$2"; shift ;;
            --user)    user="$2"; shift ;;
            --branch)  branch="$2"; shift ;;
            --) shift; setup_args=("$@"); break ;;
            *) die "init: unknown option $1" ;;
        esac
        shift
    done
    ensure_local
    [ -n "$address" ] || read -r -p "Server address (IP or hostname): " address </dev/tty
    [ -n "$address" ] || die "no address"
    [ -n "$name" ] || read -r -p "Server name (e.g. boba): " name </dev/tty
    [[ $name =~ ^[A-Za-z0-9_-]{1,32}$ ]] || die "server name must be 1-32 of [A-Za-z0-9_-]: '$name'"
    if entries | awk -v n="$name" '$1 == n { f = 1 } END { exit !f }'; then die "'$name' is already in $LIST"; fi
    target="$user@$address"

    log "Connecting to $target"
    if ! ssh -o BatchMode=yes -o ConnectTimeout=10 "$target" true 2>/dev/null; then
        info "If ssh asks about the host key, compare the fingerprint with your provider's"
        info "console before answering yes."
        ssh "${SSH_OPTS[@]}" -o BatchMode=no "$target" true || die "cannot log in to $target"
        if ! ssh -o BatchMode=yes -o ConnectTimeout=10 "$target" true 2>/dev/null; then
            local reply=""
            read -r -p "    Log in with your SSH key from now on (ssh-copy-id)? [Y/n] " reply </dev/tty || true
            if [[ -z $reply || $reply =~ ^[Yy] ]]; then ssh-copy-id "$target" || die "ssh-copy-id failed"; fi
        fi
    fi
    probe=$(ssh "${SSH_OPTS[@]}" "$target" '
        echo "uid=$(id -u)"
        . /etc/os-release 2>/dev/null && echo "os=$ID $VERSION_ID"
        [ -x '"$DEF_DIR"'/wg-setup.sh ] && echo installed=1
        sed -n "s/^SERVER_NAME=/saved=/p" /etc/wireguard/wg-tools.state 2>/dev/null | tail -n 1') || die "cannot reach $target"
    [[ $probe == *"uid=0"* ]] || die "log in as root (wg-setup.sh needs it): --user root"
    os=$(sed -n 's/^os=//p' <<<"$probe")
    info "$address: ${os:-unknown OS}"
    case "$os" in debian\ *|ubuntu\ *) ;; *) die "unsupported OS '$os' (Debian 11+ or Ubuntu 20.04+)" ;; esac

    saved=$(sed -n 's/^saved=//p' <<<"$probe")
    if [[ $probe == *"installed=1"* ]] && [ -n "$saved" ]; then
        log "wireguard-tools is already set up there as '$saved'"
        if [ "$saved" != "$name" ]; then
            info "recording it as '$saved' (its saved name); rename it with: $0 setup $saved --name $name"
            name="$saved"
            if entries | awk -v n="$name" '$1 == n { f = 1 } END { exit !f }'; then die "'$name' is already in $LIST"; fi
        fi
        record "$name" "$target"
        return 0
    fi

    url=$(repo_url)
    log "Installing git and cloning $url ($branch) to $DEF_DIR"
    ssh "${SSH_OPTS[@]}" "$target" "set -e
        export DEBIAN_FRONTEND=noninteractive
        command -v git >/dev/null || { apt-get update -qq && apt-get install -y -qq git >/dev/null; }
        if [ -d $DEF_DIR/.git ]; then git -C $DEF_DIR fetch -q && git -C $DEF_DIR checkout -q $(q "$branch") && git -C $DEF_DIR pull -q
        else git clone -q -b $(q "$branch") $(q "$url") $DEF_DIR; fi
        git -C $DEF_DIR log --oneline -1" || die "clone failed"

    log "Running wg-setup.sh setup --name $name on $address"
    rsh "$target" "$DEF_DIR/wg-setup.sh setup --name $(q "$name") $(q "${setup_args[@]+"${setup_args[@]}"}")" \
        || die "setup did not finish; fix the cause and re-run: $0 init --address $address --name $name"
    record "$name" "$target"
    info "next: $0 client $name add <client>   (or push exit cities: $0 push-exits $name)"
}

record() {
    ensure_local
    printf '%-10s %s\n' "$1" "$2" >>"$LIST"
    mkdir -p "$LOCAL/exits/$1"
    chmod 700 "$LOCAL/exits/$1"
    log "Added '$1' ($2) to $LIST"
}

# --- per-server commands (called with: name target dir [args]) ---

do_ssh()    { rsh "$2"; }
do_pull()   { ssh "${SSH_OPTS[@]}" "$2" "git -C $3 pull -q && git -C $3 log --oneline -1 | sed 's/^/    /'"; }
do_branch() {
    [ -n "${4:-}" ] || die "usage: $0 branch <server> <branch>"
    ssh "${SSH_OPTS[@]}" "$2" "set -e; git -C $3 fetch -q; git -C $3 checkout -q $(q "$4"); git -C $3 pull -q; printf '    %s: ' \"\$(git -C $3 rev-parse --abbrev-ref HEAD)\"; git -C $3 log --oneline -1"
}
do_wgsetup() { local name="$1" target="$2" dir="$3"; shift 3; rsh "$target" "$dir/wg-setup.sh $(q "$@")"; }
do_client()  { local name="$1" target="$2" dir="$3"; shift 3; rsh "$target" "$dir/wg-client.sh $(q "$@")"; }

do_backup() {
    local name="$1" target="$2" dir="$3" out to
    out=$(ssh "${SSH_OPTS[@]}" "$target" "$dir/wg-setup.sh backup --yes >/dev/null 2>&1; ls -t /var/backups/wireguard-tools/wireguard-tools.*.tar.gz 2>/dev/null | head -n 1") || true
    [ -n "$out" ] || { printf 'ERROR: %s: backup failed\n' "$name" >&2; return 1; }
    to="$LOCAL/backups/$name"
    ensure_local
    mkdir -p "$to" && chmod 700 "$to"
    scp -q -p "${SSH_OPTS[@]}" "$target:$out" "$to/" || return 1
    chmod 600 "$to/$(basename "$out")"
    info "$name: $to/$(basename "$out") (private keys inside; restore with wg-setup.sh restore)"
}

# The link for a local exit config: "code<TAB>full name" (empty if none yet).
link_of() { awk -F'\t' -v s="$1" -v f="$2" '$1 == s && $2 == f { print $3 "\t" $4; exit }' "$LINKS" 2>/dev/null; }

# First push of a local config: ask what it is and pick its code on the
# server (city's first 3 letters + Proton's country tag, e.g. par-fr).
make_link() {
    local server="$1" f="$2" file name hint cc city code reply
    file=$(basename "$f")
    hint=$(awk '/^[[:space:]]*\[Peer\]/ { p = 1; next } p && /^[[:space:]]*#/ { sub(/^[[:space:]]*#[[:space:]]*/, ""); print; exit }' "$f")
    [ -t 0 ] || die "$file has no link yet; run push-exits in a terminal once to name it"
    read -r -p "What is $file (Proton ${hint:-?})? e.g. Paris, France: " name </dev/tty || true
    [ -n "$name" ] || die "no name for $file"
    cc=$(sed -n 's/^\([A-Za-z][A-Za-z]\)#.*/\1/p' <<<"$hint" | tr 'A-Z' 'a-z')
    city=$(tr 'A-Z' 'a-z' <<<"$name" | tr -cd 'a-z' | cut -c1-3)
    code="$city${cc:+-$cc}"
    while :; do
        read -r -p "Short code on the server [$code]: " reply </dev/tty || true
        code="${reply:-$code}"
        if [[ $code =~ ^[a-z][a-z0-9-]{0,4}[a-z0-9]$ ]] && [ "$code" != warp ]; then break; fi
        echo "  2-6 of a-z0-9 and -, starting with a letter (e.g. par-fr)"
    done
    printf '%s\t%s\t%s\t%s\n' "$server" "$file" "$code" "$name" >>"$LINKS"
    chmod 600 "$LINKS"
}

do_push_exits() {
    local name="$1" target="$2" dir="$3" src f file link code label files=() inbox=/etc/wireguard/exits/incoming reply
    shift 3
    src="$LOCAL/exits/$name"
    if [ $# -gt 0 ]; then
        for file in "$@"; do files+=("$src/${file%.conf}.conf"); done
    else
        for f in "$src"/*.conf; do [ -f "$f" ] && files+=("$f"); done
    fi
    [ ${#files[@]} -gt 0 ] || die "no configs in $src (save Proton WireGuard configs there, any name ending .conf)"
    ssh "${SSH_OPTS[@]}" "$target" "install -d -m 700 /etc/wireguard/exits $inbox" || die "cannot reach $target"
    for f in "${files[@]}"; do
        [ -f "$f" ] || die "missing $f"
        chmod 600 "$f"
        file=$(basename "$f")
        link=$(link_of "$name" "$file")
        if [ -z "$link" ]; then make_link "$name" "$f"; link=$(link_of "$name" "$file"); fi
        code="${link%%$'\t'*}"
        label="${link#*$'\t'}"
        log "$name: $file -> exit $code ($label)"
        scp -q -p "${SSH_OPTS[@]}" "$f" "$target:$inbox/$code.conf" || die "upload failed"
        ssh "${SSH_OPTS[@]}" "$target" "$dir/wg-setup.sh exit add $(q "$code" "$inbox/$code.conf" --label "$label" --yes); rc=\$?; rm -f $inbox/$code.conf; exit \$rc" \
            || die "exit add failed for $code (nothing left in $inbox)"
    done
    log "$name exits"
    ssh "${SSH_OPTS[@]}" "$target" "$dir/wg-setup.sh exit list"
}

cmd_forget() {
    local line name reply="" sel="${1:-}"
    [ "$sel" != --yes ] || sel=""
    line=$(pick "$sel") || exit 1
    name="${line%% *}"
    if [[ " $* " == *" --yes "* ]]; then reply=y; else
        read -r -p "    Remove '$name' from $LIST? (nothing on the server changes) [y/N] " reply </dev/tty || true
    fi
    [[ $reply =~ ^[Yy] ]] || { info "kept"; return 0; }
    awk -v n="$name" '$1 != n' "$LIST" >"$LIST.new" && mv "$LIST.new" "$LIST" && chmod 600 "$LIST"
    info "forgot '$name'; its local/exits/$name and local/backups/$name stay"
}

usage() { sed -n '/^# Usage:/,/^# macOS/p' "$0" | sed 's/^# \{0,1\}//'; }

cmd="${1:-list}"
[ $# -eq 0 ] || shift
case "$cmd" in
    list)       cmd_list "$@" ;;
    init)       cmd_init "$@" ;;
    ssh)        each "${1:-}" do_ssh ;;
    pull)       each "${1:-}" do_pull ;;
    branch)     sel="${1:-}"; [ $# -eq 0 ] || shift; each "$sel" do_branch "$@" ;;
    setup|warp|status|test|exit|log|rollback)
                sel="${1:-}"; [ $# -eq 0 ] || shift; each "$sel" do_wgsetup "$cmd" "$@" ;;
    client)     sel="${1:-}"; [ $# -eq 0 ] || shift; each "$sel" do_client "$@" ;;
    push-exits) sel="${1:-}"; [ $# -eq 0 ] || shift; each "$sel" do_push_exits "$@" ;;
    backup)     each "${1:-}" do_backup ;;
    forget)     cmd_forget "$@" ;;
    help|-h|--help) usage ;;
    *) die "unknown command: $cmd (see: $0 help)" ;;
esac
