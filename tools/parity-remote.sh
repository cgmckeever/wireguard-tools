#!/usr/bin/env bash
# parity-remote.sh <dir> — the server side of tools/parity.sh: render with the
# last bash version (<dir>/bash, from git tag bash-final) and with the Python
# copy in <dir>, then diff (keys redacted). Read-only.
set -uo pipefail
tmp="$1"
# The Python version words three comments differently (it names wgt, not the
# old scripts); bash's output is reworded the same way so the rest compares
# byte for byte.
wording() { sed -E 's/\(use wg-client\.sh\)/(use wgt client)/; s/disabled \(wg-client\.sh enable /disabled (wgt client enable /; s/WARP setting: wg-setup\.sh warp on\|off/WARP setting: wgt warp on|off/'; }
red() { sed -E 's/^([[:space:]]*(PrivateKey|PresharedKey)[[:space:]]*=[[:space:]]*).*/\1<redacted>/'; }
[ -d "$tmp/bash" ] || { echo "    no $tmp/bash (tools/parity.sh unpacks git tag bash-final there)"; exit 1; }
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
) 2>&1 | wording >"$tmp/bash.out"
python3 "$tmp/bin/wgt" debug >"$tmp/python.out" 2>&1
if diff -u "$tmp/bash.out" "$tmp/python.out" >"$tmp/diff"; then
    echo "    identical ($(grep -c '^===' "$tmp/bash.out") pieces, $(wc -l <"$tmp/bash.out") lines)"
else
    echo "    DIFFERENT:"
    sed 's/^/    /' "$tmp/diff" | cut -c1-160 | head -30
    exit 1
fi
