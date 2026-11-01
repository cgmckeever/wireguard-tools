"""Client commands against a copy of a fixture server, with commands recorded
by FakeRunner instead of run."""

from __future__ import annotations

import os
import shutil

import pytest
from conftest import FIXTURES

from wgt import cli, clients, store
from wgt.const import Paths
from wgt.errors import WgtError
from wgt.host import Facts
from wgt.system import FakeRunner, Out

KEYS = {
    "wg genkey": (0, "CLIENTPRIVATEKEYxxxxxxxxxxxxxxxxxxxxxxxxxxx=\n"),
    "wg pubkey": (0, "CLIENTPUBLICKEYyyyyyyyyyyyyyyyyyyyyyyyyyyyy=\n"),
    "wg genpsk": (0, "CLIENTPRESHAREDKEYzzzzzzzzzzzzzzzzzzzzzzzzz=\n"),
    "ip link show dev wg0": (0, ""),          # wg0 is up
    "ip link show dev warp": (0, ""),
}


@pytest.fixture
def server(tmp_path):
    def make(case="exits", argv=(), answers=None):
        wg = tmp_path / "wg"
        if not wg.exists():
            shutil.copytree(os.path.join(FIXTURES, case, "wg"), wg)
            (wg / "server.pub").write_text("SERVERPUBLICKEY=\n")
            (wg / "wg0.conf").write_text("# Managed by wireguard-tools\n")
        paths = Paths(wg_dir=str(wg), backup_dir=str(tmp_path / "bak"), lock_file=str(tmp_path / "lock"))
        runner = FakeRunner(dict(KEYS, **(answers or {})), out=Out())
        facts = Facts(kernel_ipv6=True, has_ipv6=True, hostname="boba", routes4="", default_route="")
        args = cli.parse(list(argv) + ["--yes", "--no-qr"])
        return cli.load(args, paths=paths, runner=runner, env={}, facts=facts)
    return make


def test_add_with_exit_puts_rules_before_peer(server):
    ctx = server(argv=["--exit", "warp,par-fr,par-fr"])
    clients.cmd_add(ctx, ["phone"])
    names = [c.name for c in ctx.snap.clients]
    assert names.count("phone") == 1 and names.count("par-fr.phone") == 1   # deduplicated
    calls = ctx.runner.changes()
    rule = next(i for i, c in enumerate(calls) if "rule add from 10.2.10.7/32 lookup 201" in c)
    peer = next(i for i, c in enumerate(calls) if c.startswith("wg set wg0 peer CLIENTPUBLICKEY") and "10.2.10.7/32" in c)
    assert rule < peer
    conf = store.read_text(clients.client_conf(ctx, "par-fr.phone"))
    assert "# exit: par-fr = Paris, France (Proton -> Cloudflare WARP)" in conf
    assert "Address = 10.2.10.7/32, fdfc:cd08:368e::7/128" in conf


def test_add_unknown_exit(server):
    ctx = server(argv=["--exit", "zz-qq"])
    with pytest.raises(WgtError, match="no exit city 'zz-qq'"):
        clients.cmd_add(ctx, ["phone"])


def test_removal_targets_spare_exit_clients(tmp_path):
    for f in ["boba.laptop.conf", "boba.par-fr.laptop.conf", "laptop.conf", "laptop.conf.2026.bak",
              "old.laptop.conf.20260101-000000.bak", "boba.par-fr.laptop.conf.2026.bak", "boba.laptop2.conf"]:
        (tmp_path / f).write_text("")
    got = sorted(os.path.basename(p) for p in clients.removal_targets("laptop", str(tmp_path)))
    assert got == ["boba.laptop.conf", "laptop.conf", "laptop.conf.2026.bak", "old.laptop.conf.20260101-000000.bak"]
    got = sorted(os.path.basename(p) for p in clients.removal_targets("par-fr.laptop", str(tmp_path)))
    assert got == ["boba.par-fr.laptop.conf", "boba.par-fr.laptop.conf.2026.bak"]


def test_remove_takes_peer_out_before_rules(server):
    ctx = server()
    c = ctx.snap.client("par-fr.mbp-1")
    clients.cmd_remove(ctx, ["par-fr.mbp-1"])
    calls = ctx.runner.changes()
    peer = calls.index(f"wg set wg0 peer {c.pub} remove")
    rule = next(i for i, x in enumerate(calls) if "rule del from 10.2.10.3/32" in x)
    assert peer < rule
    assert ctx.snap.client("par-fr.mbp-1") is None


def test_rename_keeps_keys(server):
    ctx = server()
    before = ctx.snap.client("par-fr.mbp-1")
    os.makedirs(ctx.paths.client_dir, exist_ok=True)
    open(clients.client_conf(ctx, "par-fr.mbp-1"), "w").write(
        "# wireguard-tools client: par-fr.mbp-1 on boba\n# exit: x\n[Interface]\nPrivateKey = K\n")
    clients.cmd_rename(ctx, ["par-fr.mbp-1", "nyc-us.laptop"])
    after = ctx.snap.client("nyc-us.laptop")
    assert (after.ip, after.pub) == (before.ip, before.pub)
    conf = store.read_text(clients.client_conf(ctx, "nyc-us.laptop"))
    assert conf.startswith("# wireguard-tools client: nyc-us.laptop on boba\n# exit: nyc-us = New York, US")
    assert not os.path.exists(clients.client_conf(ctx, "par-fr.mbp-1"))
    calls = ctx.runner.changes()
    assert any("rule del from 10.2.10.3/32 lookup 201" in x for x in calls)
    assert any("rule add from 10.2.10.3/32 lookup 203" in x for x in calls)


def test_disable_and_enable(server):
    ctx = server()
    clients.cmd_disable(ctx, ["mbp-1"], True)
    assert ctx.snap.is_disabled("mbp-1")
    assert "# mbp-1 (10.2.10.2): disabled" in store.read_text(ctx.cfg.wg_conf)
    clients.cmd_disable(ctx, ["mbp-1"], False)
    assert not ctx.snap.is_disabled("mbp-1")


def test_dry_run_changes_nothing(server):
    ctx = server(argv=["--dry-run"])
    before = store.read_text(ctx.paths.client_db)
    clients.cmd_add(ctx, ["phone"])
    assert store.read_text(ctx.paths.client_db) == before
    assert not [c for c in ctx.runner.calls if c.startswith("wg set")]


def test_list_shows_device_first(server, capsys):
    from wgt import checks
    ctx = server()
    names = checks.print_clients(ctx, numbered=True)
    shown = [line.split()[1:3] for line in capsys.readouterr().out.splitlines()[1:]]
    # grouped by device, default exit first; the full names come back for the menu
    assert shown == [["mbp-1", "warp"], ["mbp-1", "par-fr"], ["phone", "nyc-us"], ["tv", "par-fr"]]
    assert names == ["mbp-1", "par-fr.mbp-1", "nyc-us.phone", "par-fr.tv"]


def _conf(ctx, name):
    os.makedirs(ctx.paths.client_dir, exist_ok=True)
    with open(clients.client_conf(ctx, name), "w") as f:
        f.write(f"# wireguard-tools client: {name} on boba\n# exit: x\n[Interface]\nPrivateKey = K\n")


def test_rename_dry_run_with_wg0_up(server):
    ctx = server(argv=["--dry-run"])
    _conf(ctx, "mbp-1")
    before = store.read_text(ctx.paths.client_db)
    clients.cmd_rename(ctx, ["mbp-1", "par-fr.x"])
    assert store.read_text(ctx.paths.client_db) == before


def test_rename_between_exits_adds_before_removing(server):
    ctx = server()
    _conf(ctx, "par-fr.mbp-1")
    clients.cmd_rename(ctx, ["par-fr.mbp-1", "nyc-us.mbp-1"])
    calls = ctx.runner.changes()
    add_new = next(i for i, c in enumerate(calls) if "rule add from 10.2.10.3/32 lookup 203" in c)
    del_old = next(i for i, c in enumerate(calls) if "rule del from 10.2.10.3/32 lookup 201" in c)
    assert add_new < del_old


def test_rename_same_exit_leaves_rules(server):
    ctx = server()
    _conf(ctx, "par-fr.mbp-1")
    clients.cmd_rename(ctx, ["par-fr.mbp-1", "par-fr.laptop"])
    assert not [c for c in ctx.runner.changes() if " rule " in c]


def test_rename_without_psk_refused(server):
    ctx = server()
    _conf(ctx, "mbp-1")
    os.remove(os.path.join(ctx.paths.psk_dir, "mbp-1.psk"))
    with pytest.raises(WgtError, match="no preshared key"):
        clients.cmd_rename(ctx, ["mbp-1", "laptop"])
    assert ctx.snap.client("mbp-1") is not None


def test_add_rereads_files_after_lock(server):
    ctx = server()
    with open(ctx.paths.client_db, "a") as f:   # another command added a client meanwhile
        f.write("late 10.2.10.6 LATEKEY 0.0.0.0/0,::/0\n")
    clients.cmd_add(ctx, ["new"])
    assert ctx.snap.client("new").ip != "10.2.10.6"


def test_exit_remove_refused_while_used_and_ordered(server):
    from wgt import exits
    ctx = server()
    with pytest.raises(WgtError, match="clients use exit 'par-fr'"):
        exits.cmd_remove(ctx, ["par-fr"])
    clients.cmd_remove(ctx, ["nyc-us.phone"])
    ctx.runner.calls.clear()
    exits.cmd_remove(ctx, ["nyc-us"])
    calls = ctx.runner.changes()
    stop = next(i for i, c in enumerate(calls) if c.startswith("systemctl stop wg-quick@wx-nyc-us"))
    flush = next(i for i, c in enumerate(calls) if "route flush table 203" in c)
    assert stop < flush
    assert ctx.snap.exit("nyc-us") is None and ctx.snap.exit("par-fr") is not None


def test_warp_switch_is_live_without_restart(server):
    from wgt import server as srv
    ctx = server()
    open(ctx.cfg.wg_conf, "w").write("# Managed by wireguard-tools (WARP on)\n")
    srv.cmd_warp(ctx, ["off"])
    calls = ctx.runner.changes()
    assert any("rule del from 10.2.10.0/24 lookup 100 priority 10000" in c for c in calls)
    assert not any("restart wg-quick@wg0" in c for c in calls)
    assert store.state_get(store.read_text(ctx.paths.state), "WARP") == "off"
