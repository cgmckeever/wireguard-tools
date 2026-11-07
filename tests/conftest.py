"""Shared test helpers: load a fixture server (tests/fixtures/<case>/wg) the way
a command would."""

from __future__ import annotations

import dataclasses
import os
import re
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wgt import cli, store  # noqa: E402
from wgt.config import resolve  # noqa: E402
from wgt.const import SETTING_KEYS, Paths  # noqa: E402
from wgt.host import Facts  # noqa: E402
from wgt.system import FakeRunner, Out  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
GOLDEN = os.path.join(ROOT, "tests", "golden")
CASES = sorted(os.listdir(FIXTURES))


def temp_paths(root, **chosen) -> Paths:
    """Paths with every field under <root> (wgcf_bin at <root>/wgcf, run_dir at <root>/run,
    ... unless <chosen>), so a field added to Paths can't send a test to the real system."""
    def where(name):
        return str(chosen.get(name, os.path.join(str(root), re.sub(r"_(dir|file|bin)$", "", name))))
    return Paths(**{f.name: where(f.name) for f in dataclasses.fields(Paths)})


class Server:
    """A fixture server: its paths, config, snapshot and public interface."""

    def __init__(self, case: str, wg_dir: str = ""):
        self.case = case
        self.wg_dir = wg_dir or os.path.join(FIXTURES, case, "wg")
        self.paths = Paths(wg_dir=self.wg_dir)
        self.snap = store.load(self.paths)
        saved = store.saved_settings(self.snap.state, SETTING_KEYS)
        self.cfg = resolve({}, {}, saved, paths=self.paths)
        self.cfg.finalize(kernel_ipv6=True)
        self.pub_if = store.state_get(self.snap.state, "PUB_IF")

    def norm(self, text: str) -> str:
        """Write the fixture's path as /etc/wireguard, as the golden files do."""
        return text.replace(self.wg_dir, "/etc/wireguard")


def golden(case: str, name: str) -> str:
    with open(os.path.join(GOLDEN, case, name), encoding="utf-8") as f:
        return f.read()


def assert_golden(case: str, name: str, got: str) -> None:
    """<got> must equal tests/golden/<case>/<name>. With UPDATE_GOLDEN=1 the
    file is rewritten instead (for a deliberate rendering change: review the
    git diff before committing)."""
    if os.environ.get("UPDATE_GOLDEN") == "1":
        with open(os.path.join(GOLDEN, case, name), "w", encoding="utf-8") as f:
            f.write(got)
        return
    assert got == golden(case, name), f"{case}/{name} differs from the golden file"


def golden_names(case: str):
    return sorted(os.listdir(os.path.join(GOLDEN, case)))


@pytest.fixture(params=CASES)
def server(request):
    return Server(request.param)


# What key generation returns, and wg0 and warp up: a client command's world.
KEYS = {
    "wg genkey": (0, "CLIENTPRIVATEKEYxxxxxxxxxxxxxxxxxxxxxxxxxxx=\n"),
    "wg pubkey": (0, "CLIENTPUBLICKEYyyyyyyyyyyyyyyyyyyyyyyyyyyyy=\n"),
    "wg genpsk": (0, "CLIENTPRESHAREDKEYzzzzzzzzzzzzzzzzzzzzzzzzz=\n"),
    "ip link show dev wg0": (0, ""),
    "ip link show dev warp": (0, ""),
}


@pytest.fixture
def make_ctx(tmp_path):
    """A factory: the Ctx a command would get, on a copy of a fixture server in
    tmp_path, with a FakeRunner answering from <answers>. A command's defaults
    (KEYS, --yes, --no-qr) come unless bare=True (the watchdogs run with none).
    Called twice in a test, it reuses the same copy."""
    def make(case="exits", argv=(), answers=None, bare=False, has_ipv6=True):
        wg = tmp_path / "wg"
        if not wg.exists():
            shutil.copytree(os.path.join(FIXTURES, case, "wg"), wg)
            (wg / "server.pub").write_text("SERVERPUBLICKEY=\n")
            (wg / "wg0.conf").write_text("# Managed by wireguard-tools\n")
        run = tmp_path / "run"
        run.mkdir(exist_ok=True)
        paths = temp_paths(tmp_path)
        base = {} if bare else KEYS
        runner = FakeRunner(dict(base, **(answers or {})), out=Out())
        facts = Facts(kernel_ipv6=True, has_ipv6=has_ipv6, hostname="boba", routes4="", default_route="")
        flags = [] if bare else ["--yes", "--no-qr"]
        return cli.load(cli.parse(list(argv) + flags), paths=paths, runner=runner, env={}, facts=facts)
    return make
