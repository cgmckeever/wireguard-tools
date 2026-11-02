#!/usr/bin/env bash
# render-diff.sh — Before deploying: what would this checkout render differently
# from what each server's installed wgt renders, from that server's real state?
# Read-only: copies this checkout to a temp dir there, runs `wgt debug` with
# both (keys redacted), diffs, and removes the temp dir.
#
#   tools/render-diff.sh                   every server in local/servers
#   tools/render-diff.sh root@1.2.3.4 ...  just these
#
# "identical" means a pull changes no config there. A difference is what the
# next `wgt setup` on that server would write (and possibly restart for).
set -uo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
targets=("$@")
if [ ${#targets[@]} -eq 0 ]; then
    while read -r name target _; do
        case "$name" in ""|\#*) continue ;; esac
        targets+=("$target")
    done <"$here/local/servers"
fi
rc=0
for target in "${targets[@]}"; do
    echo "==> $target"
    tmp=$(ssh -n "$target" 'mktemp -d') || { echo "    unreachable"; rc=1; continue; }
    if ! (cd "$here" && COPYFILE_DISABLE=1 tar --no-xattrs -cf - wgt bin templates 2>/dev/null) \
            | ssh "$target" "tar -xf - -C $tmp"; then
        echo "    upload failed"; rc=1; ssh -n "$target" "rm -rf $tmp"; continue
    fi
    # shellcheck disable=SC2016
    ssh "$target" bash -s -- "$tmp" <<'REMOTE' || rc=1
tmp="$1"
installed=$(dirname "$(dirname "$(readlink -f /usr/local/sbin/wgt)")")
WGT_SKIP_PREFLIGHT=1 python3 "$installed/bin/wgt" debug >"$tmp/installed.out" 2>&1
WGT_SKIP_PREFLIGHT=1 python3 "$tmp/bin/wgt" debug >"$tmp/new.out" 2>&1
if diff -u "$tmp/installed.out" "$tmp/new.out" >"$tmp/diff"; then
    echo "    identical ($(grep -c '^===' "$tmp/new.out") pieces, $(wc -l <"$tmp/new.out") lines)"
else
    echo "    DIFFERENT from $installed:"
    sed 's/^/    /' "$tmp/diff" | cut -c1-160 | head -40
    exit 1
fi
REMOTE
    ssh -n "$target" "rm -rf $tmp"
done
exit "$rc"
