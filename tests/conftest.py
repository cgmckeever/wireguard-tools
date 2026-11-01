"""Shared test helpers: load a fixture server (tests/fixtures/<case>/wg) the way
a command would."""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wgt import store  # noqa: E402
from wgt.config import resolve  # noqa: E402
from wgt.const import CONFIG_KEYS, Paths  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
GOLDEN = os.path.join(ROOT, "tests", "golden")
CASES = sorted(os.listdir(FIXTURES))


class Server:
    """A fixture server: its paths, config, snapshot and public interface."""

    def __init__(self, case: str, wg_dir: str = ""):
        self.case = case
        self.wg_dir = wg_dir or os.path.join(FIXTURES, case, "wg")
        self.paths = Paths(wg_dir=self.wg_dir)
        self.snap = store.load(self.paths)
        saved = store.saved_settings(self.snap.state, list(CONFIG_KEYS) + ["ENDPOINT"])
        self.cfg = resolve({}, {}, saved, paths=self.paths)
        self.cfg.finalize(kernel_ipv6=True)
        self.pub_if = store.state_get(self.snap.state, "PUB_IF")

    def norm(self, text: str) -> str:
        """Write the fixture's path as /etc/wireguard, as the golden files do."""
        return text.replace(self.wg_dir, "/etc/wireguard")


def golden(case: str, name: str) -> str:
    with open(os.path.join(GOLDEN, case, name), encoding="utf-8") as f:
        return f.read()


def golden_names(case: str):
    return sorted(os.listdir(os.path.join(GOLDEN, case)))


@pytest.fixture(params=CASES)
def server(request):
    return Server(request.param)
