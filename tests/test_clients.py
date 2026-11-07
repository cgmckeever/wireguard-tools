"""Client commands against a copy of a fixture server, with commands recorded
by FakeRunner instead of run."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from wgt import clients, store
from wgt.errors import WgtError


def test_add_with_exit_puts_rules_before_peer(make_ctx):
    ctx = make_ctx(argv=["--exit", "host,par-fr,par-fr", "--warp", "on"])
    clients.cmd_add(ctx, ["phone"])
    names = [c.name for c in ctx.snap.clients]
    assert names.count("warp.phone") == 1 and names.count("warp.par-fr.phone") == 1   # deduplicated
    calls = ctx.runner.changes()
    rule = next(i for i, c in enumerate(calls) if "rule add from 10.2.10.7/32 lookup 201" in c)
    peer = next(i for i, c in enumerate(calls)
                if c.startswith("wg set wg0 peer CLIENTPUBLICKEY") and "10.2.10.7/32" in c)
    assert rule < peer
    conf = store.read_text(clients.client_conf(ctx, "warp.par-fr.phone"))
    assert "# exit: par-fr = Paris, France (jump -> Cloudflare WARP)" in conf
    assert "Address = 10.2.10.7/32, fdfc:cd08:368e::7/128" in conf


def test_add_unknown_exit(make_ctx):
    ctx = make_ctx(argv=["--exit", "zz-qq"])
    with pytest.raises(WgtError, match="no jump 'zz-qq'"):
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
    c = ctx.snap.client("warp.par-fr.mbp-1")
    clients.cmd_remove(ctx, ["warp.par-fr.mbp-1"])
    calls = ctx.runner.changes()
    peer = calls.index(f"wg set wg0 peer {c.pub} remove")
    rule = next(i for i, x in enumerate(calls) if "rule del from 10.2.10.3/32" in x)
    assert peer < rule
    assert ctx.snap.client("warp.par-fr.mbp-1") is None


def test_rename_keeps_keys(make_ctx):
    ctx = make_ctx()
    before = ctx.snap.client("warp.par-fr.mbp-1")
    os.makedirs(ctx.paths.client_dir, exist_ok=True)
    Path(clients.client_conf(ctx, "warp.par-fr.mbp-1")).write_text(
        "# wireguard-tools client: warp.par-fr.mbp-1 on boba\n# exit: x\n[Interface]\nPrivateKey = K\n")
    clients.cmd_rename(ctx, ["warp.par-fr.mbp-1", "warp.nyc-us.laptop"])
    after = ctx.snap.client("warp.nyc-us.laptop")
    assert (after.ip, after.pub) == (before.ip, before.pub)
    conf = store.read_text(clients.client_conf(ctx, "warp.nyc-us.laptop"))
    assert conf.startswith("# wireguard-tools client: warp.nyc-us.laptop on boba\n# exit: nyc-us = New York, US")
    assert not os.path.exists(clients.client_conf(ctx, "warp.par-fr.mbp-1"))
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


def test_list_shows_configs_by_device(make_ctx, capsys):
    ctx = make_ctx()
    names = clients.print_clients(ctx, numbered=True)
    out = capsys.readouterr().out.splitlines()
    assert out[0].split()[1:5] == ["DEVICE", "CONFIG", "EXIT", "WARP"]
    # grouped by device: the host's configs first, then the jumps'
    assert names == ["mbp-1", "warp.par-fr.mbp-1", "nyc-us.phone", "warp.par-fr.tv"]
    rows = {line.split()[2]: line for line in out[1:]}
    assert rows["warp.par-fr.tv"].split()[1] == "tv"
    assert rows["mbp-1"].split()[3:5] == ["host", "off"]
    assert "Paris, France" in rows["warp.par-fr.mbp-1"] and " on " in rows["warp.par-fr.mbp-1"]


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
    _conf(ctx, "warp.par-fr.mbp-1")
    clients.cmd_rename(ctx, ["warp.par-fr.mbp-1", "warp.nyc-us.mbp-1"])
    calls = ctx.runner.changes()
    add_new = next(i for i, c in enumerate(calls) if "rule add from 10.2.10.3/32 lookup 203" in c)
    del_old = next(i for i, c in enumerate(calls) if "rule del from 10.2.10.3/32 lookup 201" in c)
    assert add_new < del_old


def test_rename_same_exit_leaves_rules(make_ctx):
    ctx = make_ctx()
    _conf(ctx, "warp.par-fr.mbp-1")
    clients.cmd_rename(ctx, ["warp.par-fr.mbp-1", "warp.par-fr.laptop"])
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
    with pytest.raises(WgtError, match="configs use jump 'par-fr'"):
        exits.cmd_remove(ctx, ["par-fr"])
    clients.cmd_remove(ctx, ["nyc-us.phone"])
    ctx.runner.calls.clear()
    exits.cmd_remove(ctx, ["nyc-us"])
    calls = ctx.runner.changes()
    stop = next(i for i, c in enumerate(calls) if c.startswith("systemctl stop wg-quick@wx-nyc-us"))
    flush = next(i for i, c in enumerate(calls) if "route flush table 203" in c)
    assert stop < flush
    assert ctx.snap.exit("nyc-us") is None and ctx.snap.exit("par-fr") is not None


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
    with pytest.raises(WgtError, match=r"unknown jump command: nope \(list, add, remove, test;"):
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
    assert labels == ["WARP", "tunnels", "settings", "route", "firewall", "watchdog", "jumps", "clients"]
    assert any("par-fr (Paris, France)" in line for line in out)


# --- names say the way out: [warp.][<jump>.]<device> ---

def test_add_makes_plain_and_warp_per_exit(make_ctx):
    ctx = make_ctx(argv=["--exit", "host,par-fr"])
    clients.cmd_add(ctx, ["mac"])
    for name in ("mac", "warp.mac", "par-fr.mac", "warp.par-fr.mac"):
        assert ctx.snap.client(name), name
    ip = {n: ctx.snap.client(n).ip for n in ("mac", "warp.mac", "par-fr.mac", "warp.par-fr.mac")}
    calls = ctx.runner.changes()
    main = next(i for i, c in enumerate(calls) if f"rule add from {ip['mac']}/32 lookup main priority 9500" in c)
    stop = next(i for i, c in enumerate(calls) if f"rule add from {ip['mac']}/32 priority 9501 blackhole" in c)
    peer = next(i for i, c in enumerate(calls) if c.startswith("wg set wg0 peer") and f"{ip['mac']}/32" in c)
    assert stop < main < peer   # the blackhole first: never a moment without it
    assert not any(f"from {ip['warp.mac']}/32" in c for c in calls if " rule " in c)   # the subnet's WARP rule
    assert any(f"rule add from {ip['par-fr.mac']}/32 lookup 231 priority 9500" in c for c in calls)
    assert any(f"rule add from {ip['warp.par-fr.mac']}/32 lookup 201 priority 9500" in c for c in calls)
    conf = lambda n: store.read_text(clients.client_conf(ctx, n))   # noqa: E731
    assert "# exit: this server's IP (no WARP)" in conf("mac")
    assert "# exit: this server -> Cloudflare WARP" in conf("warp.mac")
    assert "# exit: par-fr = Paris, France (jump, no WARP)" in conf("par-fr.mac")
    assert "# exit: par-fr = Paris, France (jump -> Cloudflare WARP)" in conf("warp.par-fr.mac")
    assert clients.exit_short("warp.par-fr.mac") == "par-fr" and clients.exit_short("warp.mac") == "host"
    assert clients.way_out(ctx, "par-fr.mac") == "jump:Paris, France, no WARP"
    assert clients.exit_label(ctx, "warp.mac") == "host"   # no LOCATION saved
    ctx.snap.state += "LOCATION=Atlanta\n"
    assert clients.exit_label(ctx, "warp.mac") == "host:Atlanta"


def test_add_one_warp_setting(make_ctx):
    ctx = make_ctx(argv=["--exit", "host,host", "--warp", "on"])
    clients.cmd_add(ctx, ["phone2"])
    names = [c.name for c in ctx.snap.clients]
    assert names.count("warp.phone2") == 1 and "phone2" not in names


def test_add_warp_off_only(make_ctx):
    ctx = make_ctx(argv=["--warp", "off"])
    clients.cmd_add(ctx, ["phone3"])
    names = [c.name for c in ctx.snap.clients]
    assert "phone3" in names and "warp.phone3" not in names


def test_add_full_config_name(make_ctx):
    ctx = make_ctx()
    clients.cmd_add(ctx, ["warp.par-fr.tablet"])
    assert [c.name for c in ctx.snap.clients if "tablet" in c.name] == ["warp.par-fr.tablet"]
    ctx = make_ctx(argv=["--warp", "on"])
    with pytest.raises(WgtError, match="full config name"):
        clients.cmd_add(ctx, ["warp.tablet"])


def test_warp_flag_values(make_ctx):
    with pytest.raises(WgtError, match="--warp takes on, off or on,off"):
        make_ctx(argv=["--warp", "maybe"])
    assert make_ctx(argv=["--warp", "on,off"]).args.warp == "on,off"


def test_rename_out_of_warp_adds_before_removing(make_ctx):
    """Renaming is how a config changes its WARP: warp.par-fr.mbp-1 -> par-fr.mbp-1
    moves it from the jump's WARP table (201) to the jump table (231), the jump
    table's IPv6 blackhole and NAT first (a wg0 running older hooks may lack them)."""
    ctx = make_ctx()
    _conf(ctx, "warp.par-fr.mbp-1")
    clients.cmd_rename(ctx, ["warp.par-fr.mbp-1", "par-fr.mbp-1"])
    calls = ctx.runner.changes()
    v6_stop = next(i for i, c in enumerate(calls) if "ip -6 route replace blackhole default metric 4096 table 231" in c)
    nat = next(i for i, c in enumerate(calls) if "-o px-par-fr -j MASQUERADE" in c)
    add_new = next(i for i, c in enumerate(calls) if "rule add from 10.2.10.3/32 lookup 231" in c)
    del_old = next(i for i, c in enumerate(calls) if "rule del from 10.2.10.3/32 lookup 201" in c)
    assert v6_stop < add_new and nat < add_new and add_new < del_old
    assert "# exit: par-fr = Paris, France (jump, no WARP)" in store.read_text(clients.client_conf(ctx, "par-fr.mbp-1"))
    assert "lookup 231 priority 9500" in store.read_text(ctx.cfg.wg_conf)


def test_rename_into_host_warp_drops_its_rules(make_ctx):
    ctx = make_ctx()   # mbp-1: the host, no WARP
    _conf(ctx, "mbp-1")
    clients.cmd_rename(ctx, ["mbp-1", "warp.mbp-1"])
    calls = ctx.runner.changes()
    assert any("rule del from 10.2.10.2/32 lookup main priority 9500" in c for c in calls)
    assert any("rule del from 10.2.10.2/32 priority 9501 blackhole" in c for c in calls)
    assert not any("rule add" in c for c in calls)


def test_rename_host_to_plain_jump(make_ctx):
    ctx = make_ctx()
    _conf(ctx, "mbp-1")
    clients.cmd_rename(ctx, ["mbp-1", "par-fr.mac"])
    calls = ctx.runner.changes()
    add_new = next(i for i, c in enumerate(calls) if "rule add from 10.2.10.2/32 lookup 231" in c)
    del_old = next(i for i, c in enumerate(calls) if "rule del from 10.2.10.2/32 lookup main" in c)
    assert add_new < del_old


def test_remove_plain_host_config(make_ctx):
    ctx = make_ctx()
    clients.cmd_remove(ctx, ["mbp-1"])
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
    # nyc-us.phone has no WARP: a rule to its WARP table (203) instead of the jump's (233) fails
    wrong = v4.replace("lookup 233", "lookup 203")
    ctx = make_ctx(answers={"ip rule show": (0, wrong), "ip -6 rule show": (0, v6)})
    assert not exits.client_rules_ok(ctx, ctx.snap.exit("nyc-us"))
    no_stop = v4.replace("9501:\tfrom 10.2.10.2 blackhole\n", "")
    ctx = make_ctx(answers={"ip rule show": (0, no_stop)})
    assert not checks.Tests(ctx).direct_rules_ok("ip")


def test_a_digit_int_refuses_is_a_word_not_a_traceback(make_ctx, monkeypatch):
    """"²" is a digit to str.isdigit, and int() raises on it: an answer like that at
    the exit menu must read as a jump code, not crash."""
    monkeypatch.setattr(clients.ui, "say", lambda text: None)
    monkeypatch.setattr(clients.ui, "ask", lambda prompt: "²")
    assert clients.pick_exits(make_ctx(), "tv") == ["²"]
