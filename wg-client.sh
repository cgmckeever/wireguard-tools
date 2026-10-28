#!/usr/bin/env bash
# wg-client.sh — Manage WireGuard clients on a server set up by wg-setup.sh.
# Each client gets a keypair, a preshared key and the next free address; its
# config is written to /root/clients/<server>.<name>.conf. See README.md.

set -Eeuo pipefail

PROG=$(basename "$0")
SELF=$(readlink -f "${BASH_SOURCE[0]}")
# shellcheck source=lib/common.sh
. "$(dirname "$SELF")/lib/common.sh"

# Print help.
usage() {
    cat <<EOF
Usage: $PROG [options] <command> [name]

Manages clients of the WireGuard server set up by wg-setup.sh. Changes apply
to the running server immediately; other clients stay connected.

Commands:
  add <name>      New client: keypair, preshared key, next free address,
                  config at $CLIENT_DIR/$SERVER_NAME.<name>.conf
  rotate [name]   Issue new keys for an existing client (same address); the old
                  config stops working
  remove [name]   Remove the client, its keys and its config
  list            Clients with address, allowed IPs, last handshake, traffic
  show [name]     Config path and contents with keys redacted (--qr for a QR code)
  share [name]    Send the config with magic-wormhole: prints a one-time code;
                  the recipient runs: wormhole receive <code>
  help            This text

Options:
  --allowed-ips <cidrs>  With add/rotate: what the client sends through the VPN.
                         Default (set at server setup): $CLIENT_ALLOWED_IPS
                         e.g. "$WG_SUBNET$( [ "$IPV6" -eq 1 ] && echo ", $WG_SUBNET6")" for VPN-network-only (split tunnel)
  --endpoint <host>      With add/rotate: override the server address in the config
  --qr / --no-qr         With add/rotate: QR code of the new config (default: on
                         when run in a terminal); with show: add one (to re-send).
                         It contains the client's private key. In iTerm2/WezTerm
                         it is drawn as an image QR_WIDTH columns wide (default
                         32; QR_WIDTH=0 for text)
  --dry-run              Show what would change; change nothing
  --yes, -y              Don't prompt

Leave out [name] to pick from a numbered list.
Names: 1-32 characters of A-Z a-z 0-9 _ -
EOF
}

valid_name() {
    [[ $1 =~ ^[A-Za-z0-9_-]{1,32}$ ]] || die "client name must be 1-32 of [A-Za-z0-9_-]: '$1'"
}

# No name given: list clients and ask for a number or name (needs a terminal).
pick_client() {
    local names=() name ip _ i reply=""
    while read -r name ip _; do names+=("$name"); done < <(db_entries)
    [ ${#names[@]} -gt 0 ] || die "no clients yet (add one: $PROG add <name>)"
    if ! have_tty || [ "$YES" -eq 1 ]; then die "usage: $PROG $1 <name>"; fi
    {
        echo ""
        for i in "${!names[@]}"; do
            printf '  %2d) %-20s %s\n' $(( i + 1 )) "${names[$i]}" "$(db_get "${names[$i]}" | awk '{ print $1 }')"
        done
        echo ""
    } >/dev/tty
    read -r -p "  $1 which client? [number or name] " reply </dev/tty || true
    if [[ $reply =~ ^[0-9]+$ ]] && [ "$reply" -ge 1 ] && [ "$reply" -le ${#names[@]} ]; then
        echo "${names[$(( reply - 1 ))]}"
    elif [ -n "$reply" ] && [ -n "$(db_get "$reply")" ]; then
        echo "$reply"
    else
        die "no such client: '${reply}'"
    fi
}

# The server must be set up first (wg-setup.sh direct or warp).
require_server() {
    [ -n "$SAVED_EXIT" ] && [ -f "$WG_CONF" ] && [ -s "$SERVER_PUB" ] && return 0
    [ "$DRY_RUN" -eq 1 ] && { warn "server not set up yet; showing what would happen"; return 0; }
    die "no WireGuard server here yet; run 'wg-setup.sh direct' or 'wg-setup.sh warp' first"
}

# First unused host address in the subnet (the server takes .1).
next_free_ip() {
    local used n ip
    used=" $SERVER_IP $(db_entries | awk '{ printf "%s ", $2 }')"
    for (( n = FIRST_HOST; n <= LAST_HOST; n++ )); do
        ip=$(int2ip "$n")
        [[ $used == *" $ip "* ]] || { echo "$ip"; return 0; }
    done
    die "no free addresses left in $WG_SUBNET"
}

# Send a client's config with magic-wormhole; the receiver runs
# `wormhole receive <code>`. One-time code; gives up after 10 minutes.
cmd_share() {
    local name="${1:-}" conf
    [ -n "$name" ] || name=$(pick_client share) || exit 1
    valid_name "$name"
    conf=$(client_conf "$name")
    [ -f "$conf" ] || die "no config for '$name' (see: $PROG list)"
    command -v wormhole >/dev/null || die "magic-wormhole not installed (apt-get install magic-wormhole)"
    if [ "$DRY_RUN" -eq 1 ]; then
        info "[dry-run] would run: wormhole send $conf"
        return 0
    fi
    warn "this sends '$name''s private key; give the code only to that person"
    info "receiver runs:  wormhole receive <code>   (then imports $(basename "$conf") into WireGuard)"
    info "waiting up to 10 minutes; Ctrl-C cancels"
    # --foreground keeps wormhole in the terminal's process group, so Ctrl-C reaches it.
    timeout --foreground 600 wormhole send --code-length 3 "$conf" || die "not received (cancelled or timed out); nothing was sent"
}

# Print a config as a terminal QR code. Comments, blank lines and padding spaces
# are left out and the margin is 1 module, to keep the code small. In iTerm2 or
# WezTerm (inline images) it is drawn as a QR_WIDTH-cell-wide image, streamed so
# the key never touches disk; QR_WIDTH=0 forces text.
show_qr() {
    local width="${QR_WIDTH:-32}"
    command -v qrencode >/dev/null || die "qrencode not installed"
    [[ $width =~ ^[0-9]+$ ]] || die "QR_WIDTH must be a number of terminal columns: '$width'"
    warn "this QR code contains the client's private key"
    if [ "$width" -gt 0 ] && [ -t 1 ] && [ -z "${TMUX:-}" ] && [[ "${LC_TERMINAL:-} ${TERM_PROGRAM:-}" =~ iTerm2|iTerm.app|WezTerm ]]; then
        printf '\033]1337;File=inline=1;width=%s;preserveAspectRatio=1:%s\a\n' "$width" \
            "$(qr_data "$1" | qrencode -t PNG -s 4 -m 2 -o - | base64 -w0)"
    else
        qr_data "$1" | qrencode -t ansiutf8 -m 1
    fi
}

# Config lines that matter, without comments or padding, for a smaller QR code.
qr_data() {
    grep -v -E '^[[:space:]]*(#|$)' "$1" | sed -E 's/[[:space:]]*=[[:space:]]*/=/; s/,[[:space:]]+/,/g'
}

# Generate keys for <name> at <ip> with <allowed> (comma list), write its config,
# and update clients.db, wg0.conf and the live interface. <old_pub> is replaced.
issue_client() {
    local name="$1" ip="$2" allowed="$3" old_pub="${4:-}" conf priv pub psk content
    conf=$(client_conf "$name")
    if [ "$DRY_RUN" -eq 1 ]; then
        info "[dry-run] would generate a keypair + preshared key, write $conf (mode 600),"
        info "[dry-run] and add the peer to $WG_CONF and live $WG_IF (AllowedIPs ${allowed//,/, })"
        return 0
    fi
    install -d -m 700 "$CLIENT_DIR" "$PSK_DIR"
    priv=$(wg genkey)
    pub=$(wg pubkey <<<"$priv")
    psk=$(wg genpsk)
    # Render first: a template error must stop us before anything is written.
    content=$(render_template client.conf.tmpl \
        NAME="$name" SERVER_NAME="$SERVER_NAME" PRIVATE_KEY="$priv" ADDRESS="$(client_addrs "$ip" | sed 's/,/, /')" DNS="$WG_DNS" MTU="$WG_MTU" \
        SERVER_PUBLIC_KEY="$(cat "$SERVER_PUB")" PRESHARED_KEY="$psk" \
        ENDPOINT="$ENDPOINT:$WG_PORT" ALLOWED_IPS="${allowed//,/, }")
    write_file "$PSK_DIR/$name.psk" 600 <<<"$psk"
    write_file "$conf" 600 <<<"$content"
    content=""
    priv=""
    psk=""
    db_put "$name" "$ip $pub $allowed"
    write_wg_conf
    if if_exists "$WG_IF"; then
        if [ -n "$old_pub" ] && [ "$old_pub" != "$pub" ]; then wg set "$WG_IF" peer "$old_pub" remove; fi
        wg set "$WG_IF" peer "$pub" preshared-key "$PSK_DIR/$name.psk" allowed-ips "$(client_addrs "$ip")"
        info "peer live on $WG_IF (other clients unaffected)"
    else
        warn "$WG_IF is not up; the peer is in $WG_CONF and loads when it starts"
    fi
    # QR by default when a person is watching; --qr forces it, --no-qr skips it.
    if [ "$QR" -eq 1 ]; then
        show_qr "$conf"
    elif [ "$QR" -eq 0 ] && [ -t 1 ]; then
        if command -v qrencode >/dev/null; then show_qr "$conf"; else warn "qrencode not installed; no QR code"; fi
    fi
    echo ""
    info "client config: $conf"
    info "fetch it with: scp root@$(tr -d '[]' <<<"$ENDPOINT"):$conf ."
}

# --allowed-ips as a comma list without spaces.
allowed_list() { tr -d '[:space:]' <<<"$1"; }

cmd_add() {
    local name="${1:-}" existing ip
    [ -n "$name" ] || die "usage: $PROG add <name>"
    valid_name "$name"
    take_lock
    require_server
    existing=$(db_get "$name")
    if [ -n "$existing" ]; then
        info "client '$name' already exists (${existing%% *}); config: $(client_conf "$name")"
        info "use '$PROG rotate $name' to issue new keys"
        return 0
    fi
    ip=$(next_free_ip)
    resolve_endpoint 0
    step "Add client '$name' at $ip" "$PROG remove $name"
    issue_client "$name" "$ip" "$(allowed_list "$CLIENT_ALLOWED_IPS")"
}

cmd_rotate() {
    local name="${1:-}" existing ip old_pub old_allowed allowed
    [ -n "$name" ] || name=$(pick_client rotate) || exit 1
    valid_name "$name"
    take_lock
    require_server
    existing=$(db_get "$name")
    [ -n "$existing" ] || die "no client '$name' (see: $PROG list)"
    read -r ip old_pub old_allowed <<<"$existing"
    # Keep the client's own AllowedIPs unless --allowed-ips was given.
    allowed=$(allowed_list "$CLIENT_ALLOWED_IPS")
    if [ "$ALLOWED_IPS_GIVEN" -eq 0 ] && [ -n "$old_allowed" ]; then
        allowed="$old_allowed"
    fi
    resolve_endpoint 0
    step "Rotate keys for '$name' ($ip); its current config stops working" "none (old keys are discarded)"
    confirm "Rotate keys for $name?"
    issue_client "$name" "$ip" "$allowed" "$old_pub"
}

cmd_remove() {
    local name="${1:-}" existing pub
    [ -n "$name" ] || name=$(pick_client remove) || exit 1
    valid_name "$name"
    take_lock
    existing=$(db_get "$name")
    if [ -z "$existing" ]; then
        info "no client '$name'"
        return 0
    fi
    pub=$(awk '{ print $2 }' <<<"$existing")
    step "Remove client '$name' (${existing%% *})" "$PROG add $name (issues new keys)"
    confirm "Remove $name?"
    if if_exists "$WG_IF"; then run wg set "$WG_IF" peer "$pub" remove; fi
    db_put "$name"
    # After rollback no mode is saved; the next setup rebuilds wg0.conf from clients.db.
    if [ -n "$SAVED_EXIT" ]; then write_wg_conf; else info "server not set up; $WG_CONF is rebuilt at the next setup"; fi
    # The config, preshared key and their .bak copies all hold this client's secrets.
    # Also any copy left under an older server name or from before server names,
    # and copies older versions kept next to the files.
    local d
    # shellcheck disable=SC2086  # globs on purpose
    for d in "$CLIENT_DIR" "$(change_dir "$CLIENT_DIR/x")"; do
        run rm -f $d/*.$name.conf $d/*.$name.conf.*.bak $d/$name.conf $d/$name.conf.*.bak
    done
    # shellcheck disable=SC2086
    for d in "$PSK_DIR" "$(change_dir "$PSK_DIR/x")"; do
        run rm -f $d/$name.psk $d/$name.psk.*.bak
    done
}

cmd_list() {
    local -A hs=() rx=() tx=()
    local k a b name ip pub allowed
    if if_exists "$WG_IF"; then
        while read -r k a; do hs[$k]="$a"; done < <(wg show "$WG_IF" latest-handshakes)
        while read -r k a b; do rx[$k]="$a"; tx[$k]="$b"; done < <(wg show "$WG_IF" transfer)
    fi
    printf '%-20s %-15s %-14s %-12s %s\n' NAME ADDRESS "LAST SEEN" "RX/TX MiB" "ALLOWED IPS"
    while read -r name ip pub allowed; do
        printf '%-20s %-15s %-14s %-12s %s\n' "$name" "$ip" "$(ago "${hs[$pub]:-0}")" \
            "$(( ${rx[$pub]:-0} / 1048576 ))/$(( ${tx[$pub]:-0} / 1048576 ))" "${allowed//,/, }"
    done < <(db_entries)
}

cmd_show() {
    local name="${1:-}" conf
    [ -n "$name" ] || name=$(pick_client show) || exit 1
    valid_name "$name"
    conf=$(client_conf "$name")
    [ -f "$conf" ] || die "no config for '$name' (see: $PROG list)"
    info "$conf"
    echo ""
    redact <"$conf"
    if [ "$QR" -eq 1 ]; then
        echo ""
        show_qr "$conf"
    fi
}

main() {
    init "$@"
    set -- "${ARGS[@]}"
    local cmd="$1"
    shift
    case "$cmd" in
        add)    cmd_add "$@" ;;
        rotate) cmd_rotate "$@" ;;
        remove) cmd_remove "$@" ;;
        list)   cmd_list ;;
        show)   cmd_show "$@" ;;
        share)  cmd_share "$@" ;;
        *) die "unknown command: $cmd (see: $PROG help)" ;;
    esac
}

main "$@"
