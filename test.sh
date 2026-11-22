#!/usr/bin/env bash
# test.sh — Run the tests (and lint) on this machine (no root, no server needed).
# The first run creates .venv/ with pytest, ruff and mypy; later runs reuse it.
#   ./test.sh            all tests
#   ./test.sh -k exit    only tests whose name matches "exit" (any pytest option works)
#   ./test.sh lint       ruff and mypy (settings in pyproject.toml)
#   ./test.sh all        lint, then the tests (the pre-commit hook and CI run this)
#   ./test.sh hooks      use .githooks/ (the pre-commit hook) in this clone
set -euo pipefail
cd "$(dirname "$0")"
if [ "${1:-}" = hooks ]; then
    git config core.hooksPath .githooks
    echo "pre-commit hook on: each commit runs ./test.sh all first"
    exit 0
fi
RUFF=0.17.4     # also in pyproject.toml (CI runs this script, so it uses this pin)
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c 'import pytest, ruff, mypy' 2>/dev/null \
    || [ "$(.venv/bin/ruff --version)" != "ruff $RUFF" ]; then
    echo "==> Setting up .venv with pytest, ruff and mypy (first run only)"
    python3 -m venv .venv
    .venv/bin/pip install -q pytest "ruff==$RUFF" mypy
fi
lint() {
    .venv/bin/ruff check wgt tests
    .venv/bin/mypy
}
case "${1:-}" in
    lint) lint ;;
    all)  shift; lint; exec .venv/bin/python -m pytest -q tests "$@" ;;
    *)    exec .venv/bin/python -m pytest -q tests "$@" ;;
esac
