#!/usr/bin/env bash
# parity-remote.sh <dir> — the server side of tools/parity.sh: render with the
# bash tools in /opt/wireguard-tools and with the Python copy in <dir>, then
# diff (keys redacted). Read-only.
set -uo pipefail
tmp="$1"
red() { sed -E 's/^([[:space:]]*(PrivateKey|PresharedKey)[[:space:]]*=[[:space:]]*).*/\1<redacted>/'; }
cp -r /opt/wireguard-tools "$tmp/bash"
sed '$d' "$tmp/bash/wg-setup.sh" >"$tmp/bash/ws.sh"
(
    set -Eeuo pipefail
    # shellcheck disable=SC1091
    . "$tmp/bash/ws.sh"
    parse_args status
    load_config
    finalize_config
    echo "=== wg0"; render_wg_conf | red
    echo "=== warp"; render_warp_conf "$WGCF_DIR/wgcf-profile.conf" | red
    for c in $(exit_codes); do
        echo "=== px-$c"; render_proton_conf "$EXIT_DIR/$c.proton.conf" "$c" | red
        echo "=== wx-$c"; render_warp_conf "$EXIT_DIR/$c/wgcf-profile.conf" "$(exit_wtable "$c")" "$(exit_mark "$c")" | red
    done
    echo "=== hooks-down"; hook_cmds down
    echo "=== ipt"; ipt_rules
) >"$tmp/bash.out" 2>&1
python3 "$tmp/bin/wgt" debug >"$tmp/python.out" 2>&1
if diff -u "$tmp/bash.out" "$tmp/python.out" >"$tmp/diff"; then
    echo "    identical ($(grep -c '^===' "$tmp/bash.out") pieces, $(wc -l <"$tmp/bash.out") lines)"
else
    echo "    DIFFERENT:"
    sed 's/^/    /' "$tmp/diff" | cut -c1-160 | head -30
    exit 1
fi
