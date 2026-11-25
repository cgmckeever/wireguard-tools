"""Routes-only jumps (exits.routes): a jump that carries only some subnets of its
configs (a friend's home LAN), whose internet leaves from this server. On the
exits fixture, nyc-us has one WARP-off config (nyc-us.phone, 10.2.10.4; jump
table 233) and par-fr has WARP ones."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pytest
from test_server import PROTON, WgcfRunner

from wgt import checks, clients, exits, host, ops, results, store, watchdog
from wgt.errors import WgtError
from wgt.system import Out

NOW = int(time.time())
LAN = "10.20.30.0/24"
PHONE_ROUTE = f"from 10.2.10.4/32 to {LAN} lookup 233 priority 9450"
PHONE_JUMP = "from 10.2.10.4/32 lookup 233 priority 9500"
PHONE_MAIN = "from 10.2.10.4/32 lookup main priority 9500"


def index(calls, text):
    return next(i for i, c in enumerate(calls) if text in c)


def routed(make_ctx, routes=(LAN,), **kw):
    """The exits fixture with nyc-us routes-only."""
    ctx = make_ctx(**kw)
    ops.take_lock(ctx)
    ops.set_routes(ctx, "nyc-us", routes)
    ops.release_lock(ctx)
    ctx.runner.calls.clear()
    return ctx


def test_routes_change_live_new_rules_before_old_ones_go(make_ctx):
    ctx = make_ctx()
    exits.cmd_routes(ctx, ["nyc-us", LAN])
    assert ctx.snap.exit_routes("nyc-us") == (LAN,)
    assert f"nyc-us {LAN}" in Path(ctx.paths.exit_routes_db).read_text()
    calls = ctx.runner.changes()
    assert index(calls, f"rule add {PHONE_ROUTE}") < index(calls, f"rule add {PHONE_MAIN}") \
        < index(calls, f"rule del {PHONE_JUMP}")
    assert not any("systemctl restart wg-quick@wg0" in c for c in calls)   # live, nothing restarts
    conf = Path(ctx.cfg.wg_conf).read_text()
    assert f"PostUp = ip rule add {PHONE_ROUTE} 2>/dev/null || true" in conf
    assert PHONE_JUMP not in conf


def test_more_routes_add_rules_and_remove_none(make_ctx):
    ctx = routed(make_ctx)
    exits.cmd_routes(ctx, ["nyc-us", f"{LAN},192.168.7.0/24"])
    rule_calls = [c for c in ctx.runner.changes() if " rule " in c]
    assert any("rule add from 10.2.10.4/32 to 192.168.7.0/24 lookup 233 priority 9450" in c for c in rule_calls)
    assert not [c for c in rule_calls if " rule del " in c]


def test_routes_none_makes_a_normal_jump_again(make_ctx):
    ctx = routed(make_ctx)
    exits.cmd_routes(ctx, ["nyc-us", "none"])
    assert ctx.snap.exit_routes("nyc-us") == () and "nyc-us" not in Path(ctx.paths.exit_routes_db).read_text()
    calls = ctx.runner.changes()
    added = index(calls, f"rule add {PHONE_JUMP}")
    assert added < index(calls, f"rule del {PHONE_MAIN}") and added < index(calls, f"rule del {PHONE_ROUTE}")
    assert any("rule del from 10.2.10.4/32 priority 9501 blackhole" in c for c in calls)
    assert PHONE_JUMP in Path(ctx.cfg.wg_conf).read_text()


def test_routes_are_printed(make_ctx):
    ctx = make_ctx()
    got = []
    ctx.out.line = lambda text="": got.append(text)
    exits.cmd_routes(ctx, ["nyc-us"])
    ctx = routed(make_ctx, (LAN, "192.168.7.0/24"))
    ctx.out.line = lambda text="": got.append(text)
    exits.cmd_routes(ctx, ["nyc-us"])
    assert got == ["none", f"{LAN},192.168.7.0/24"]
    with pytest.raises(WgtError, match="no jump 'nope'"):
        exits.cmd_routes(ctx, ["nope"])


def test_a_jump_with_warp_configs_cant_be_routes_only(make_ctx):
    ctx = make_ctx()
    with pytest.raises(WgtError, match=r"WARP on \(warp.par-fr.mbp-1 warp.par-fr.tv\)"):
        exits.cmd_routes(ctx, ["par-fr", LAN])
    assert not os.path.exists(ctx.paths.exit_routes_db)
    exits.cmd_routes(ctx, ["par-fr", "none"])   # a normal jump already: fine


@pytest.mark.parametrize("word,why", [
    ("10.2.10.0/25", "this server's client subnet"), ("10.0.0.0/8", "this server's client subnet"),
    ("10.9.8.128/25", "the devices' static addresses"), ("0.0.0.0/0", "every address"),
    ("10.20.30.1/24", "IPv4 network"), ("fd00::/64", "IPv4 network"), ("lan", "IPv4 network"),
])
def test_routes_are_checked(make_ctx, word, why):
    ctx = make_ctx()
    with pytest.raises(WgtError, match=why):
        exits.cmd_routes(ctx, ["nyc-us", word])
    assert not os.path.exists(ctx.paths.exit_routes_db)


def test_jump_add_with_routes(make_ctx, tmp_path, monkeypatch):
    """A new routes-only jump: exits.routes written, and its check brings up the jump
    tunnel alone and waits for its handshake, tracing nothing through it."""
    monkeypatch.setattr(host, "resolve4", lambda name: None)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    src = tmp_path / "lan.conf"
    src.write_text(PROTON)
    ctx = make_ctx(argv=["--label", "Pat's house", "--routes", LAN],
                   answers={"ip link show dev px-lan": (1, ""), "ip link show dev wx-lan": (1, ""),
                            "wg show px-lan latest-handshakes": (0, f"K\t{NOW - 5}\n")})
    Path(ctx.paths.wgcf_bin).write_text("#!/bin/sh\n")
    Path(ctx.paths.wgcf_bin).chmod(0o755)
    ctx.runner = WgcfRunner(ctx.runner.answers, out=Out())
    ctx.files.runner = ctx.runner
    exits.cmd_add(ctx, ["lan", str(src)])
    assert ctx.snap.exit("lan").label == "Pat's house" and ctx.snap.exit_routes("lan") == (LAN,)
    calls = ctx.runner.changes()
    assert "systemctl restart wg-quick@px-lan" in calls
    assert not any("wg-quick@wx-lan" in c and c.startswith("systemctl restart") for c in calls)
    assert not any(c.startswith("curl") for c in calls)


def test_jump_add_with_routes_refuses_warp_configs_and_bad_routes(make_ctx, tmp_path):
    src = tmp_path / "x.conf"
    src.write_text(PROTON)
    with pytest.raises(WgtError, match="WARP on"):
        exits.cmd_add(make_ctx(argv=["--routes", LAN]), ["par-fr", str(src)])
    with pytest.raises(WgtError, match="client subnet"):
        exits.cmd_add(make_ctx(argv=["--routes", "10.2.10.0/24"]), ["lan", str(src)])


def test_client_add_on_a_routes_only_jump_makes_the_warp_off_config(make_ctx):
    ctx = routed(make_ctx, argv=["--exit", "nyc-us"])
    clients.cmd_add(ctx, ["tab"])
    assert ctx.snap.client("nyc-us.tab") and not ctx.snap.client("warp.nyc-us.tab")
    calls = ctx.runner.changes()
    assert any(f"to {LAN} lookup 233 priority 9450" in c and "rule add" in c for c in calls)
    conf = Path(clients.client_conf(ctx, "nyc-us.tab")).read_text()
    assert f"# exit: nyc-us = New York, US (jump for {LAN} only, no WARP)" in conf


@pytest.mark.parametrize("argv,words", [(["--exit", "nyc-us", "--warp", "on"], ["tab"]),
                                        ([], ["warp.nyc-us.tab"])])
def test_client_add_with_warp_on_a_routes_only_jump_is_refused(make_ctx, argv, words):
    ctx = routed(make_ctx, argv=argv)
    with pytest.raises(WgtError, match="routes-only"):
        clients.cmd_add(ctx, words)
    assert not ctx.snap.client("warp.nyc-us.tab") and ctx.snap.device_number("tab") is None


def test_rename_into_warp_on_a_routes_only_jump_is_refused(make_ctx):
    ctx = routed(make_ctx)
    with pytest.raises(WgtError, match="routes-only"):
        clients.cmd_rename(ctx, ["mbp-1", "warp.nyc-us.mbp-1"])


def test_jump_list_shows_the_routes_after_the_name(make_ctx, capsys):
    """tunnel-puck reads a jump's line by its first field and its state by
    (up, no WARP|partly up|idle|up)( \\([0-9]+ active\\))?: both stay as they were."""
    ctx = routed(make_ctx, answers={"ip link show dev px-nyc-us": (0, ""), "ip link show dev wx-nyc-us": (1, ""),
                                    "wg show wg0 latest-handshakes": (0, "")})
    capsys.readouterr()
    exits.cmd_list(ctx, [])
    lines = capsys.readouterr().out.splitlines()
    line = next(x for x in lines if x.split()[0] == "nyc-us")
    assert line == f"    nyc-us  New York, US (routes {LAN}) up, no WARP (0 active)   1        ?"
    assert re.search(r"(up, no WARP|partly up|idle|up)( \([0-9]+ active\))?", line).group(0) == "up, no WARP (0 active)"
    par = next(x for x in lines if x.split()[0] == "par-fr")
    assert "(routes" not in par


def test_summary_shows_the_routes(make_ctx):
    ctx = routed(make_ctx)
    assert any(f"nyc-us (New York, US (routes {LAN}))" in text for _, text in checks.summary_lines(ctx))


def test_watchdog_brings_up_only_the_jump_tunnel(make_ctx):
    ctx = routed(make_ctx, bare=True, has_ipv6=False,
                 answers={"ip link show dev wg0": (0, ""), "ip link show dev px-nyc-us": (1, ""),
                          "ip link show dev wx-nyc-us": (1, ""), "ip link show dev px-par-fr": (1, ""),
                          "ip link show dev wx-par-fr": (1, "")})
    ctx.clock = lambda: NOW
    ctx.runner.answers["wg show wg0 latest-handshakes"] = (0, f"{ctx.snap.client('nyc-us.phone').pub}\t{NOW - 30}\n")
    watchdog.run_exits(ctx)
    assert "systemctl restart wg-quick@px-nyc-us" in ctx.runner.calls
    assert not any("wx-nyc-us" in c and c.startswith("systemctl") for c in ctx.runner.calls)


def test_a_routes_only_jump_never_needs_its_warp():
    snap = store.Snapshot(None, [store.Client("warp.lan.tv", "10.0.0.2", "K")], [], [store.Exit("lan", 1, "LAN")],
                          routes={"lan": (LAN,)})
    assert not exits.warp_needed(snap, "lan", snap.clients)
    assert exits.warp_needed(store.Snapshot(None, snap.clients, [], snap.exits), "lan", snap.clients)


def test_jump_test_checks_the_tunnel_and_traces_nothing(make_ctx, monkeypatch):
    monkeypatch.setattr(exits, "wait_hs", lambda ctx, ifc, seconds=0: None)
    ctx = routed(make_ctx, answers={"ip link show dev px-nyc-us": (1, ""), "ip link show dev wx-nyc-us": (1, ""),
                                    "wg show px-nyc-us latest-handshakes": (0, f"K\t{NOW - 5}\n")})
    ctx.clock = lambda: NOW
    got = []
    exits.run_tests(ctx, results.Results(out=got.append), bring_up=True, only="nyc-us")
    assert any(g.split()[0] == "SKIP" and f"routes-only ({LAN})" in g for g in got), got
    assert any("handshake" in g and "px-nyc-us" in g for g in got)
    assert any(f"{LAN} via the jump, the rest via this server's IP" in g for g in got)
    calls = ctx.runner.calls
    assert not any(c.startswith("curl") for c in calls)
    assert "systemctl restart wg-quick@px-nyc-us" in calls
    assert not any(c.startswith("systemctl restart") and "wx-nyc-us" in c for c in calls)


def test_tunnel_test_skips_a_routes_only_jump(make_ctx, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    ctx = routed(make_ctx, answers={"curl -fsS --max-time 10 --interface*": (0, "ip=104.28.1.1\nwarp=on\n"),
                                    "curl -fsS --max-time 10 https*": (0, "ip=192.0.2.1\nwarp=off\n")})
    got = []
    ctx.out.line = lambda text="": got.append(text)
    exits.cmd_tunnel_test(ctx, [])
    assert any(g.split()[0] == "SKIP" and "jump:New York, US" in g and "routes-only" in g for g in got)
    assert not any("--interface px-nyc-us" in c or "--interface wx-nyc-us" in c for c in ctx.runner.calls)


def test_jump_remove_drops_its_routes(make_ctx):
    ctx = routed(make_ctx, answers={"ip link show dev px-nyc-us": (1, ""), "ip link show dev wx-nyc-us": (1, "")})
    ops.take_lock(ctx)
    ops.set_routes(ctx, "lon-gb", ("192.168.7.0/24",))   # another line stays
    ops.release_lock(ctx)
    clients.cmd_remove(ctx, ["nyc-us.phone"])
    exits.cmd_remove(ctx, ["nyc-us"])
    assert store.parse_routes(Path(ctx.paths.exit_routes_db).read_text()) == {"lon-gb": ("192.168.7.0/24",)}
