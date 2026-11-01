#!/usr/bin/env bash
# servers.sh — replaced by bin/wgt-ctl, where the server comes first:
#   servers.sh status boba     ->  wgt-ctl boba status
#   servers.sh client boba add ->  wgt-ctl boba client add
# Kept so old habits work; it translates and runs wgt-ctl. The bash version
# is git tag bash-final.
ctl="$(cd "$(dirname "$0")/.." && pwd)/bin/wgt-ctl"
case "${1:-}" in
    ""|list|init|clients|help|-h|--help) set -- "$@" ;;
    push-exits) shift; set -- "${1:-}" exit-add "${@:2}" ;;
    *) cmd="$1"; shift; sel="${1:-}"; [ $# -eq 0 ] || shift; set -- "$sel" "$cmd" "$@" ;;
esac
echo "note: scripts/servers.sh is now wgt-ctl (server first): wgt-ctl $*" >&2
exec "$ctl" "$@"
