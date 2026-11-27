"""tpk-ctl.sh's settings in ~/wg-tunnels/fleet/pucks/<puck>/ (only the local commands:
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
    puck = store / "fleet" / "pucks" / "home"
    assert (puck / "puck.conf").is_file() and (puck / "wifi.conf").is_file()
    assert mode(store / "fleet" / "pucks") == mode(puck) == "0o700"
    assert mode(puck / "puck.conf") == mode(puck / "wifi.conf") == "0o600"
    assert tpk(store, "new", "home").returncode != 0          # never over an existing one


def test_pucks_and_gateways_come_from_the_folders(tmp_path):
    store = tmp_path / "wg-tunnels"
    for name in ("home", "mom"):
        assert tpk(store, "new", name).returncode == 0
    gw = store / "fleet" / "pucks" / "mom" / "gateways"
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


def test_gateway_add_makes_a_private_settings_file(tmp_path):
    """A name wgt-ctl doesn't list: a tpk file to fill in, and a stop before any ssh."""
    store = tmp_path / "wg-tunnels"
    assert tpk(store, "new", "home").returncode == 0
    r = tpk(store, "home", "gateway", "add", "elsewhere")
    assert r.returncode == 0, r.stderr
    gw = store / "fleet" / "pucks" / "home" / "gateways"
    assert mode(gw) == "0o700" and mode(gw / "elsewhere.conf") == "0o600"
    assert "fill in GATEWAY_SSH" in r.stderr


def test_a_hand_made_copy_is_made_private_when_read(tmp_path):
    store = tmp_path / "wg-tunnels"
    puck = store / "fleet" / "pucks" / "home"
    puck.mkdir(parents=True)
    (puck / "puck.conf").write_text('SSH_LAN="pi@home.local"\n')
    os.chmod(puck / "puck.conf", 0o644)
    os.chmod(puck, 0o755)
    tpk(store, "home", "gateway")
    assert mode(puck) == "0o700" and mode(puck / "puck.conf") == "0o600"


def test_a_bad_jump_code_is_named_by_wgts_own_rule(tmp_path):
    """config checks a wgt gateway's jump codes with wgt (wgt_py); the message names the code."""
    store = tmp_path / "wg-tunnels"
    (store / "fleet" / "servers" / "gw").mkdir(parents=True)
    (store / "fleet" / "servers" / "gw" / "login").write_text("root@gw.example\n")
    assert tpk(store, "new", "home").returncode == 0
    (store / "fleet" / "pucks" / "home" / "gateways").mkdir()
    (store / "fleet" / "pucks" / "home" / "gateways" / "gw.conf").write_text(
        'TPK_ROUTING="wgt"\nWGT_SERVER="gw"\nJUMP_CODE="all"\n')
    r = tpk(store, "home", "config")
    assert r.returncode != 0 and "jump code 'all' won't do: 2-6 of" in r.stderr


def shell_function(name):
    """One function's text from tpk-ctl.sh (its body ends at the first column-0 '}')."""
    text = TPK.read_text()
    start = text.index(f"\n{name}() {{") + 1
    return text[start:text.index("\n}\n", start) + 3]


def test_status_alert_sees_the_configs_in_use():
    """active_names reads status's rows (CONFIG, then LAST HANDSHAKE as ago prints it)."""
    rows = "".join(f"  {name:<28} {age:<14} 15M     167M\n" for name, age in (
        ("puck1.iphone", "41s ago"), ("puck1.mac", "2m ago"), ("puck1.tv", "3m ago"), ("puck1.steve", "never")))
    script = shell_function("active_names") + "active_names\n"
    r = subprocess.run(["sh", "-c", script], input="  CONFIG  LAST HANDSHAKE RX TX\n" + rows,
                       capture_output=True, text=True)
    assert r.stdout.split() == ["puck1.iphone", "puck1.mac"]


def test_new_leaves_fleet_a_readme(tmp_path):
    store = tmp_path / "wg-tunnels"
    assert tpk(store, "new", "home").returncode == 0
    assert "pucks/<puck>/" in (store / "fleet" / "README-tools.md").read_text()
