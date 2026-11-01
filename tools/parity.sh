#!/usr/bin/env bash
# parity.sh — Read-only check that the Python version renders exactly what the
# bash version does from a live server's state: wg0.conf, warp.conf, each exit
# city's px-/wx- configs, wg0's PostDown lines and the firewall rules.
#
# Usage: tools/parity.sh <ssh-target> [...]
#
# Copies this checkout's Python package and the last bash version (git tag
# bash-final) to a temp dir on the server, renders with both, diffs with keys
# redacted, and removes the temp dir. Nothing on the server changes.
set -uo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
rc=0
for target in "$@"; do
    echo "==> $target"
    tmp=$(ssh -n "$target" 'mktemp -d') || { echo "    unreachable"; rc=1; continue; }
    (cd "$here" && COPYFILE_DISABLE=1 tar --no-xattrs -cf - wgt bin templates tools/parity-remote.sh 2>/dev/null) | ssh "$target" "tar -xf - -C $tmp"
    git -C "$here" archive --prefix=bash/ bash-final | ssh "$target" "tar -xf - -C $tmp"
    ssh -n "$target" "bash $tmp/tools/parity-remote.sh $tmp" || rc=1
    ssh -n "$target" "rm -rf $tmp"
done
exit "$rc"
