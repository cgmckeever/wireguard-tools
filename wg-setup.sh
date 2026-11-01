#!/bin/sh
# wg-setup.sh — replaced by `wgt` (same commands: setup, warp, exit, status,
# test, log, backup, restore, rollback). Kept so old habits and scripts work.
# The bash version is git tag bash-final.
echo "note: wg-setup.sh is now 'wgt' (e.g. wgt $*)" >&2
exec "$(dirname "$(readlink -f "$0")")/bin/wgt" "$@"
