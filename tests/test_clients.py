"""Client commands against a copy of a fixture server, with commands recorded
by FakeRunner instead of run."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from wgt import clients, store
from wgt.errors import WgtError


def test_add_with_exit_puts_rules_before_peer(make_ctx):
    ctx = make_ctx(argv=["--exit", "warp,par-fr,par-fr"])
    clients.cmd_add(ctx, ["phone"])
    names = [c.name for c in ctx.snap.clients]
    assert names.count("phone") == 1 and names.count("par-fr.phone") == 1   # deduplicated
    calls = ctx.runner.changes()
    rule = next(i for i, c in enumerate(calls) if "rule add from 10.2.10.7/32 lookup 201" in c)
    peer = next(i for i, c in enumerate(calls)
                if c.startswith("wg set wg0 peer CLIENTPUBLICKEY") and "10.2.10.7/32" in c)
    assert rule < peer
    conf = store.read_text(clients.client_conf(ctx, "par-fr.phone"))
    assert "# exit: par-fr = Paris, France (Proton -> Cloudflare WARP)" in conf
    assert "Address = 10.2.10.7/32, fdfc:cd08:368e::7/128" in conf


def test_add_unknown_exit(make_ctx):
    ctx = make_ctx(argv=["--exit", "zz-qq"])
    with pytest.raises(WgtError, match="no exit city 'zz-qq'"):
        clients.cmd_add(ctx, ["phone"])


def test_removal_targets_spare_exit_clients(tmp_path):
    for f in ["boba.laptop.conf", "boba.par-fr.laptop.conf", "old.laptop.conf.20260101-000000.bak",
              "boba.par-fr.laptop.conf.2026.bak", "boba.laptop2.conf"]:
        (tmp_path / f).write_text("")
    got = sorted(os.path.basename(p) for p in clients.removal_targets("laptop", str(tmp_path)))
    assert got == ["boba.laptop.conf", "old.laptop.conf.20260101-000000.bak"]   # any server name, and copies
    got = sorted(os.path.basename(p) for p in clients.removal_targets("par-fr.laptop", str(tmp_path)))
    assert got == ["boba.par-fr.laptop.conf", "boba.par-fr.laptop.conf.2026.bak"]


def test_remove_takes_peer_out_before_rules(make_ctx):
    ctx = make_ctx()
    c = ctx.snap.client("par-fr.mbp-1")
    clients.cmd_remove(ctx, ["par-fr.mbp-1"])
    calls = ctx.runner.changes()
    peer = calls.index(f"wg set wg0 peer {c.pub} remove")
    rule = next(i for i, x in enumerate(calls) if "rule del from 10.2.10.3/32" in x)
    assert peer < rule
    assert ctx.snap.client("par-fr.mbp-1") is None


def test_rename_keeps_keys(make_ctx):
    ctx = make_ctx()
    before = ctx.snap.client("par-fr.mbp-1")
    os.makedirs(ctx.paths.client_dir, exist_ok=True)
    Path(clients.client_conf(ctx, "par-fr.mbp-1")).write_text(
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


def test_disable_and_enable(make_ctx):
    ctx = make_ctx()
    clients.cmd_disable(ctx, ["mbp-1"])
    assert ctx.snap.is_disabled("mbp-1")
    assert "# mbp-1 (10.2.10.2): disabled" in store.read_text(ctx.cfg.wg_conf)
    clients.cmd_enable(ctx, ["mbp-1"])
    assert not ctx.snap.is_disabled("mbp-1")


def test_dry_run_changes_nothing(make_ctx):
    ctx = make_ctx(argv=["--dry-run"])
    before = store.read_text(ctx.paths.client_db)
    clients.cmd_add(ctx, ["phone"])
    assert store.read_text(ctx.paths.client_db) == before
    assert not [c for c in ctx.runner.calls if c.startswith("wg set")]


def test_list_shows_device_first(make_ctx, capsys):
    ctx = make_ctx()
    names = clients.print_clients(ctx, numbered=True)
    shown = [line.split()[1:3] for line in capsys.readouterr().out.splitlines()[1:]]
    # grouped by device, default exit first; the full names come back for the menu
    assert shown == [["mbp-1", "direct"], ["mbp-1", "Paris,"], ["phone", "New"], ["tv", "Paris,"]]
    assert names == ["mbp-1", "par-fr.mbp-1", "nyc-us.phone", "par-fr.tv"]


def _conf(ctx, name):
    os.makedirs(ctx.paths.client_dir, exist_ok=True)
    with open(clients.client_conf(ctx, name), "w") as f:
        f.write(f"# wireguard-tools client: {name} on boba\n# exit: x\n[Interface]\nPrivateKey = K\n")


def test_rename_dry_run_with_wg0_up(make_ctx):
    ctx = make_ctx(argv=["--dry-run"])
    _conf(ctx, "mbp-1")
    before = store.read_text(ctx.paths.client_db)
    clients.cmd_rename(ctx, ["mbp-1", "par-fr.x"])
    assert store.read_text(ctx.paths.client_db) == before


def test_rename_between_exits_adds_before_removing(make_ctx):
    ctx = make_ctx()
    _conf(ctx, "par-fr.mbp-1")
    clients.cmd_rename(ctx, ["par-fr.mbp-1", "nyc-us.mbp-1"])
    calls = ctx.runner.changes()
    add_new = next(i for i, c in enumerate(calls) if "rule add from 10.2.10.3/32 lookup 203" in c)
    del_old = next(i for i, c in enumerate(calls) if "rule del from 10.2.10.3/32 lookup 201" in c)
    assert add_new < del_old


def test_rename_same_exit_leaves_rules(make_ctx):
    ctx = make_ctx()
    _conf(ctx, "par-fr.mbp-1")
    clients.cmd_rename(ctx, ["par-fr.mbp-1", "par-fr.laptop"])
    assert not [c for c in ctx.runner.changes() if " rule " in c]


def test_rename_without_psk_refused(make_ctx):
    ctx = make_ctx()
    _conf(ctx, "mbp-1")
    os.remove(os.path.join(ctx.paths.psk_dir, "mbp-1.psk"))
    with pytest.raises(WgtError, match="no preshared key"):
        clients.cmd_rename(ctx, ["mbp-1", "laptop"])
    assert ctx.snap.client("mbp-1") is not None


def test_add_rereads_files_after_lock(make_ctx):
    ctx = make_ctx()
    with open(ctx.paths.client_db, "a") as f:   # another command added a client meanwhile
        f.write("late 10.2.10.6 LATEKEY 0.0.0.0/0,::/0\n")
    clients.cmd_add(ctx, ["new"])
    assert ctx.snap.client("new").ip != "10.2.10.6"


def test_exit_remove_refused_while_used_and_ordered(make_ctx):
    from wgt import exits
    ctx = make_ctx()
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


def test_warp_switch_is_live_without_restart(make_ctx):
    from wgt import server as srv
    ctx = make_ctx()
    Path(ctx.cfg.wg_conf).write_text("# Managed by wireguard-tools (WARP on)\n")
    srv.cmd_warp(ctx, ["off"])
    calls = ctx.runner.changes()
    assert any("rule del from 10.2.10.0/24 lookup 100 priority 10000" in c for c in calls)
    assert not any("restart wg-quick@wg0" in c for c in calls)
    assert store.state_get(store.read_text(ctx.paths.state), "WARP") == "off"


def test_issue_client_refuses_an_empty_endpoint(make_ctx):
    """A config with "Endpoint = :51820" would look fine and never connect."""
    ctx = make_ctx()
    with pytest.raises(WgtError, match="endpoint"):
        clients.issue_client(ctx, "phone", "10.2.10.9", "0.0.0.0/0", endpoint="")
    assert ctx.snap.client("phone") is None


def test_rename_dry_run_shows_one_peer(make_ctx):
    """A dry run's second write to clients.db must build on its first, or the
    wg0.conf diff keeps the old peer next to the new one."""
    ctx = make_ctx(argv=["--dry-run"])
    conf = Path(clients.client_conf(ctx, "mbp-1"))
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text("[Interface]\nPrivateKey = x\n")
    clients.cmd_rename(ctx, ["mbp-1", "zznew"])
    names = [c.name for c in ctx.snap.clients]
    assert "zznew" in names and "mbp-1" not in names


def test_extra_words_are_refused(make_ctx):
    """`wgt client remove a b` must not quietly remove only a."""
    ctx = make_ctx()
    with pytest.raises(WgtError, match="unexpected: tv"):
        clients.cmd_remove(ctx, ["nyc-us.phone", "tv"])
    assert ctx.snap.client("nyc-us.phone")


def test_unknown_verb_lists_the_verbs(make_ctx):
    from wgt import exits
    ctx = make_ctx()
    with pytest.raises(WgtError, match=r"unknown client command: nope \(list, add, show"):
        clients.cmd_client(ctx, ["nope"])
    with pytest.raises(WgtError, match=r"unknown exit command: nope \(list, add, remove, test;"):
        exits.cmd_exit(ctx, ["nope"])


def test_summary_block(make_ctx, capsys):
    """wgt summary: the server's name, then one labelled line per fact."""
    from wgt import checks
    ctx = make_ctx(argv=["--endpoint", "203.0.113.9"])
    ctx.cfg.values["ENDPOINT"] = "203.0.113.9"
    checks.cmd_summary(ctx, [])
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith(f"{ctx.cfg.SERVER_NAME}  (203.0.113.9)")
    labels = [line.split()[0] for line in out[1:] if line.startswith("    ") and line[4] != " "]
    assert labels == ["WARP", "tunnels", "settings", "route", "firewall", "watchdog", "exits", "clients"]
    assert any("par-fr (Paris, France)" in line for line in out)


# --- WARP per client (clients.nowarp): exit (host | city) x WARP (on | off) ---

def test_add_warp_off_host_and_city(make_ctx):
    ctx = make_ctx(argv=["--exit", "host,par-fr", "--warp", "off"])
    clients.cmd_add(ctx, ["mac"])
    assert ctx.snap.warp_off("mac") and ctx.snap.warp_off("par-fr.mac")
    calls = ctx.runner.changes()
    host_ip, city_ip = ctx.snap.client("mac").ip, ctx.snap.client("par-fr.mac").ip
    main = next(i for i, c in enumerate(calls) if f"rule add from {host_ip}/32 lookup main priority 9500" in c)
    stop = next(i for i, c in enumerate(calls) if f"rule add from {host_ip}/32 priority 9501 blackhole" in c)
    peer = next(i for i, c in enumerate(calls) if c.startswith("wg set wg0 peer") and f"{host_ip}/32" in c)
    assert main < stop < peer
    assert any(f"rule add from {city_ip}/32 lookup 231 priority 9500" in c for c in calls)   # Proton table
    assert not any(f"from {city_ip}/32 lookup 201" in c and "add" in c for c in calls)
    assert "# exit: this server's IP (WARP off for this client)" in store.read_text(clients.client_conf(ctx, "mac"))
    assert "(Proton, WARP off)" in store.read_text(clients.client_conf(ctx, "par-fr.mac"))
    assert clients.exit_short(ctx, "mac") == "direct"
    assert clients.exit_short(ctx, "par-fr.mac") == "par-fr/direct"
    assert clients.exit_label(ctx, "par-fr.mac") == "Paris, France (no WARP)"


def test_add_exit_warp_is_host(make_ctx):
    ctx = make_ctx(argv=["--exit", "warp,host"])
    clients.cmd_add(ctx, ["phone2"])
    assert [c.name for c in ctx.snap.clients].count("phone2") == 1
    assert not ctx.snap.warp_off("phone2")
    assert not any("phone2" in c or "lookup main" in c for c in ctx.runner.changes() if " rule " in c)


def test_warp_flag_values(make_ctx):
    with pytest.raises(WgtError, match="--warp takes on or off"):
        make_ctx(argv=["--warp", "maybe"])


def test_client_warp_switch_adds_before_removing(make_ctx):
    ctx = make_ctx()
    _conf(ctx, "par-fr.mbp-1")
    clients.cmd_warp(ctx, ["par-fr.mbp-1", "off"])
    assert ctx.snap.warp_off("par-fr.mbp-1")
    calls = ctx.runner.changes()
    add_new = next(i for i, c in enumerate(calls) if "rule add from 10.2.10.3/32 lookup 231" in c)
    del_old = next(i for i, c in enumerate(calls) if "rule del from 10.2.10.3/32 lookup 201" in c)
    assert add_new < del_old
    conf = store.read_text(clients.client_conf(ctx, "par-fr.mbp-1"))
    assert "# exit: par-fr = Paris, France (Proton, WARP off)" in conf
    assert "lookup 231 priority 9500" in store.read_text(ctx.cfg.wg_conf)


def test_client_warp_on_for_host_drops_its_rules(make_ctx):
    ctx = make_ctx()   # the fixture has mbp-1 with WARP off
    _conf(ctx, "mbp-1")
    clients.cmd_warp(ctx, ["mbp-1", "on"])
    assert not ctx.snap.warp_off("mbp-1")
    calls = ctx.runner.changes()
    assert any("rule del from 10.2.10.2/32 lookup main priority 9500" in c for c in calls)
    assert any("rule del from 10.2.10.2/32 priority 9501 blackhole" in c for c in calls)
    assert not any("rule add" in c for c in calls)
    ctx.runner.calls.clear()
    clients.cmd_warp(ctx, ["mbp-1", "on"])   # already on: nothing
    assert not ctx.runner.changes()


def test_client_warp_usage(make_ctx):
    ctx = make_ctx()
    with pytest.raises(WgtError, match="usage: wgt client warp"):
        clients.cmd_warp(ctx, ["mbp-1"])
    with pytest.raises(WgtError, match="no client"):
        clients.cmd_warp(ctx, ["nobody", "off"])


def test_rename_carries_warp_off(make_ctx):
    ctx = make_ctx()
    _conf(ctx, "mbp-1")
    clients.cmd_rename(ctx, ["mbp-1", "par-fr.mac"])
    assert ctx.snap.warp_off("par-fr.mac") and not ctx.snap.warp_off("mbp-1")
    calls = ctx.runner.changes()
    add_new = next(i for i, c in enumerate(calls) if "rule add from 10.2.10.2/32 lookup 231" in c)
    del_old = next(i for i, c in enumerate(calls) if "rule del from 10.2.10.2/32 lookup main" in c)
    assert add_new < del_old
    assert "(Proton, WARP off)" in store.read_text(clients.client_conf(ctx, "par-fr.mac"))


def test_remove_forgets_warp_off(make_ctx):
    ctx = make_ctx()
    clients.cmd_remove(ctx, ["mbp-1"])
    assert "mbp-1" not in store.parse_nowarp(store.read_text(ctx.paths.nowarp_db))
    assert any("rule del from 10.2.10.2/32 priority 9501 blackhole" in c for c in ctx.runner.changes())


def test_rule_checks_split_by_warp(make_ctx):
    from wgt import checks, exits
    v4 = ("0:\tfrom all lookup local\n9400:\tfrom all fwmark 0x5701 lookup 231\n"
          "9500:\tfrom 10.2.10.2 lookup main\n9500:\tfrom 10.2.10.3 lookup 201\n9500:\tfrom 10.2.10.4 lookup 233\n"
          "9500:\tfrom 10.2.10.5 lookup 201\n9501:\tfrom 10.2.10.2 blackhole\n10000:\tfrom 10.2.10.0/24 lookup 100\n")
    v6 = v4.replace("10.2.10.0/24", "fdfc:cd08:368e::/64").replace("from 10.2.10.", "from fdfc:cd08:368e::")
    ctx = make_ctx(answers={"ip rule show": (0, v4), "ip -6 rule show": (0, v6)})
    assert checks.Tests(ctx).direct_rules_ok("ip") and checks.Tests(ctx).direct_rules_ok("ip -6")
    assert exits.client_rules_ok(ctx, ctx.snap.exit("par-fr")) and exits.client_rules_ok(ctx, ctx.snap.exit("nyc-us"))
    # nyc-us.phone has WARP off: a rule to its WARP table (203) instead of Proton (233) fails
    wrong = v4.replace("lookup 233", "lookup 203")
    ctx = make_ctx(answers={"ip rule show": (0, wrong), "ip -6 rule show": (0, v6)})
    assert not exits.client_rules_ok(ctx, ctx.snap.exit("nyc-us"))
    no_stop = v4.replace("9501:\tfrom 10.2.10.2 blackhole\n", "")
    ctx = make_ctx(answers={"ip rule show": (0, no_stop)})
    assert not checks.Tests(ctx).direct_rules_ok("ip")
