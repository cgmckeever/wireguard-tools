#!/usr/bin/env bash
# test.sh — Run the test suite on this machine (no root, no server needed).
# The first run creates .venv/ with pytest; later runs reuse it.
#   ./test.sh            all tests
#   ./test.sh -k exit    only tests whose name matches "exit" (any pytest option works)
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c 'import pytest' 2>/dev/null; then
    echo "==> Creating .venv with pytest (first run only)"
    python3 -m venv .venv
    .venv/bin/pip install -q pytest
fi
exec .venv/bin/python -m pytest -q tests "$@"
