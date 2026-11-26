"""tpk-ctl.sh's settings in ~/wg-tunnels/pucks/<puck>/ (only the local commands:
nothing here reaches a puck or a gateway)."""

import os
import subprocess
from pathlib import Path

import pytest

TPK = Path(__file__).resolve().parent.parent / "tunnel-puck" / "scripts" / "tpk-ctl.sh"


def tpk(store, *args):
    env = dict(os.environ, WG_TUNNELS=str(store))
    return subprocess.run(["sh", str(TPK), *args], env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True)


def mode(path):
    return oct(path.stat().st_mode & 0o777)


def test_new_makes_a_private_puck_folder(tmp_path):
    store = tmp_path / "wg-tunnels"
    r = tpk(store, "new", "home", "--wifi")
    assert r.returncode == 0, r.stderr
    puck = store / "pucks" / "home"
    assert (puck / "puck.conf").is_file() and (puck / "wifi.conf").is_file()
    assert mode(store / "pucks") == mode(puck) == "0o700"
    assert mode(puck / "puck.conf") == mode(puck / "wifi.conf") == "0o600"
    assert tpk(store, "new", "home").returncode != 0          # never over an existing one


def test_pucks_and_gateways_come_from_the_folders(tmp_path):
    store = tmp_path / "wg-tunnels"
    for name in ("home", "mom"):
        assert tpk(store, "new", name).returncode == 0
    gw = store / "pucks" / "mom" / "gateways"
    gw.mkdir()
    (gw / "dengar.conf").write_text('TPK_ROUTING="wgt"\nWGT_SERVER="dengar"\n')
    r = tpk(store, "mom", "gateway")
    assert r.returncode == 0, r.stderr
    assert "dengar" in r.stdout and "mom" in r.stderr
    r = tpk(store, "nobody", "gateway")
    assert r.returncode != 0 and "home mom" in r.stderr     # the configured pucks, by folder


@pytest.mark.parametrize("store", ["relative/dir", "~/wg-tunnels"])
def test_a_store_that_isnt_absolute_is_refused(store):
    r = tpk(store, "new", "home")
    assert r.returncode != 0 and "absolute" in r.stderr
