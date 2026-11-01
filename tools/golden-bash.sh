#!/usr/bin/env bash
# golden-bash.sh — Render the golden files from the bash version, so the Python
# version's tests compare against what bash actually produced.
#
# Usage (on a Linux host, as root, with the last bash version unpacked:
# `git archive bash-final | tar -x -C <bash-checkout>`):
#   tools/golden-bash.sh <bash-checkout> <fixtures-dir> <out-dir>
#
# For each fixture (<fixtures-dir>/<case>/wg = a WireGuard directory with
# fake keys) it points a copy of the bash tools at that directory and writes
# <out-dir>/<case>/*: wg0.conf, warp.conf, px-/wx-<code>.conf, the live rule
# lists and a sample client config. The fixture's path is written as
# /etc/wireguard, so the output doesn't depend on where it ran.
set -euo pipefail
src="$1" fixtures="$2" out="$3"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

for dir in "$fixtures"/*/wg; do
    case=$(basename "$(dirname "$dir")")
    wg="$work/$case"
    rm -rf "$wg" && cp -r "$dir" "$wg"
    code="$work/code-$case"
    rm -rf "$code" && cp -r "$src" "$code"
    sed -i "s|^WG_DIR=/etc/wireguard|WG_DIR=$wg|" "$code/lib/common.sh"
    grep -q "^WG_DIR=$wg\$" "$code/lib/common.sh" || { echo "could not point the bash tools at $wg" >&2; exit 1; }
    sed '$d' "$code/wg-setup.sh" >"$code/ws.sh"
    sed '$d' "$code/wg-client.sh" | sed '1,/^\. "\$(dirname/d' >"$code/wc.sh"
    mkdir -p "$out/$case"
    WGT_CODE="$code" WGT_OUT="$out/$case" bash -c '
        set -Eeuo pipefail
        . "$WGT_CODE/ws.sh"
        . "$WGT_CODE/wc.sh"
        parse_args status
        load_config
        finalize_config
        o="$WGT_OUT"
        render_wg_conf >"$o/wg0.conf"
        render_warp_conf "$WGCF_DIR/wgcf-profile.conf" >"$o/warp.conf"
        hook_cmds up >"$o/hooks-up.txt"
        hook_cmds down >"$o/hooks-down.txt"
        ipt_rules >"$o/ipt-rules.txt"
        warp_rule_cmds on >"$o/warp-on.txt"
        warp_rule_cmds off >"$o/warp-off.txt"
        render_template wg-quick-after-warp.conf.tmpl WG_IF="$WG_IF" WARP_IF="$WARP_IF" >"$o/dropin.conf"
        for c in $(exit_codes); do
            render_proton_conf "$EXIT_DIR/$c.proton.conf" "$c" >"$o/px-$c.conf"
            render_warp_conf "$EXIT_DIR/$c/wgcf-profile.conf" "$(exit_wtable "$c")" "$(exit_mark "$c")" >"$o/wx-$c.conf"
            exit_hook_cmds "$c" up >"$o/exit-$c-up.txt"
            exit_hook_cmds "$c" down >"$o/exit-$c-down.txt"
            ipt_rules_out "wx-$c" >"$o/exit-$c-ipt.txt"
        done
        name=$(db_entries | awk "NR == 2 { print \$1 }")
        name=${name:-$(db_entries | awk "NR == 1 { print \$1 }")}
        ip=$(db_get "$name" | awk "{ print \$1 }")
        render_template client.conf.tmpl NAME="$name" SERVER_NAME="$SERVER_NAME" EXIT="$(exit_desc "$name")" \
            PRIVATE_KEY=CLIENT-PRIVATE-KEY ADDRESS="$(client_addrs "$ip" | sed "s/,/, /")" DNS="$WG_DNS" MTU="$WG_MTU" \
            SERVER_PUBLIC_KEY=SERVER-PUBLIC-KEY PRESHARED_KEY=CLIENT-PSK ENDPOINT="ENDPOINT:$WG_PORT" \
            ALLOWED_IPS="0.0.0.0/0, ::/0" >"$o/client-$name.conf"
    '
    # Fixture path -> /etc/wireguard.
    sed -i "s|$wg|/etc/wireguard|g" "$out/$case"/*
done
