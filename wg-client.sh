#!/bin/sh
# wg-client.sh — replaced by `wgt client ...`. Kept so old habits and scripts
# work. The bash version is git tag bash-final.
echo "note: wg-client.sh is now 'wgt client' (e.g. wgt client $*)" >&2
exec "$(dirname "$(readlink -f "$0")")/bin/wgt" client "$@"
