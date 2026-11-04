"""The exit and WARP watchdogs' decisions, with a fake runner standing in for
wg, ip and systemctl."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from wgt import cli, watchdog
from wgt.const import EXIT_WATCHDOG, WATCHDOG

NOW = int(time.time())


@pytest.fixture
def make(make_ctx):
    """A watchdog's Ctx: no command defaults, no IPv6; the runner answers <answers>."""
    def build(answers):
        return make_ctx(bare=True, has_ipv6=False, answers=answers)
    return build


def pub(ctx, name):
    return ctx.snap.client(name).pub


DOWN = (1, "")   # `ip link show dev X` fails: no such interface
UP = (0, "")


def test_brings_up_city_with_active_client(make):
    ctx = make({"ip link show dev px-par-fr": DOWN, "ip link show dev wx-par-fr": DOWN,
                "ip link show dev px-nyc-us": DOWN, "ip link show dev wx-nyc-us": DOWN})
    ctx.runner.answers["wg show wg0 latest-handshakes"] = (0, f"{pub(ctx, 'par-fr.mbp-1')}\t{NOW - 30}\n")
    watchdog.exits(ctx)
    calls = ctx.runner.calls
    assert "systemctl restart wg-quick@px-par-fr wg-quick@wx-par-fr" in calls
    assert not any("nyc-us" in c and c.startswith("systemctl") for c in calls)
    assert any("jump par-fr: a client is active; brought up" in c for c in calls)


def test_takes_down_idle_city(make):
    ctx = make({"ip link show dev px-par-fr": UP, "ip link show dev wx-par-fr": UP,
                "ip link show dev px-nyc-us": DOWN, "ip link show dev wx-nyc-us": DOWN})
    ctx.runner.answers["wg show wg0 latest-handshakes"] = (0, f"{pub(ctx, 'par-fr.mbp-1')}\t{NOW - 400}\n")
    watchdog.exits(ctx)
    assert "systemctl stop wg-quick@wx-par-fr wg-quick@px-par-fr" in ctx.runner.calls


def test_disabled_client_doesnt_keep_city_up(make):
    ctx = make({"ip link show dev px-par-fr": DOWN, "ip link show dev wx-par-fr": DOWN,
                "ip link show dev px-nyc-us": DOWN, "ip link show dev wx-nyc-us": DOWN})
    ctx.runner.answers["wg show wg0 latest-handshakes"] = (0, f"{pub(ctx, 'par-fr.tv')}\t{NOW - 5}\n")
    watchdog.exits(ctx)
    assert not any(c.startswith("systemctl restart") for c in ctx.runner.calls)


def test_hold_file_skips_city(make):
    ctx = make({"ip link show dev px-par-fr": UP, "ip link show dev wx-par-fr": UP,
                "ip link show dev px-nyc-us": DOWN, "ip link show dev wx-nyc-us": DOWN})
    open(os.path.join(ctx.paths.run_dir, f"{EXIT_WATCHDOG}.par-fr.hold"), "w").close()
    watchdog.exits(ctx)
    assert not any("par-fr" in c and c.startswith("systemctl") for c in ctx.runner.calls)


def test_stale_city_new_port_then_restart(make):
    ctx = make({"ip link show dev px-par-fr": UP, "ip link show dev wx-par-fr": UP,
                "ip link show dev px-nyc-us": DOWN, "ip link show dev wx-nyc-us": DOWN,
                "wg show wx-par-fr latest-handshakes": (0, f"K\t{NOW - 900}\n"),
                "wg show px-par-fr latest-handshakes": (0, f"K\t{NOW - 5}\n")})   # the jump itself is fine
    ctx.runner.answers["wg show wg0 latest-handshakes"] = (0, f"{pub(ctx, 'par-fr.mbp-1')}\t{NOW - 10}\n")
    watchdog.exits(ctx)
    assert "wg set wx-par-fr listen-port 0" in ctx.runner.calls
    started = os.path.join(ctx.paths.run_dir, f"{EXIT_WATCHDOG}.par-fr")
    Path(started).write_text(str(NOW - 600))    # the new port didn't help for a while
    watchdog.exits(ctx)
    assert "systemctl restart wg-quick@wx-par-fr" in ctx.runner.calls   # its WARP alone: the jump is fine


def test_dead_jump_restarts_whole(make):
    ctx = make({"ip link show dev px-par-fr": UP, "ip link show dev wx-par-fr": UP,
                "ip link show dev px-nyc-us": DOWN, "ip link show dev wx-nyc-us": DOWN,
                "wg show wx-par-fr latest-handshakes": (0, f"K\t{NOW - 900}\n"),
                "wg show px-par-fr latest-handshakes": (0, f"K\t{NOW - 900}\n")})
    ctx.runner.answers["wg show wg0 latest-handshakes"] = (0, f"{pub(ctx, 'par-fr.mbp-1')}\t{NOW - 10}\n")
    watchdog.exits(ctx)
    assert "systemctl restart wg-quick@px-par-fr wg-quick@wx-par-fr" in ctx.runner.calls


def test_skips_while_locked(make):
    import fcntl
    ctx = make({"ip link show dev px-par-fr": DOWN, "ip link show dev wx-par-fr": DOWN})
    ctx.runner.answers["wg show wg0 latest-handshakes"] = (0, f"{pub(ctx, 'par-fr.mbp-1')}\t{NOW}\n")
    fd = os.open(ctx.paths.lock_file, os.O_WRONLY | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        watchdog.exits(ctx)
    finally:
        os.close(fd)
    assert ctx.runner.calls == []


def test_warp_watchdog_acts_on_second_stale_run(make):
    ctx = make({"ip link show dev warp": UP, "wg show warp latest-handshakes": (0, f"K\t{NOW - 900}\n"),
                "wg show warp listen-port": (0, "40000\n")})
    watchdog.warp(ctx)
    assert "wg set warp listen-port 0" not in ctx.runner.calls
    assert os.path.exists(os.path.join(ctx.paths.run_dir, WATCHDOG))
    watchdog.warp(ctx)
    assert "wg set warp listen-port 0" in ctx.runner.calls


def test_watchdog_error_is_logged_not_raised(make, monkeypatch):
    from wgt import watchdog as wd

    def boom(ctx):
        raise ValueError("bad wg output")
    monkeypatch.setattr(wd, "exits", boom)
    monkeypatch.setenv("WGT_SKIP_PREFLIGHT", "1")
    ctx = make({})
    monkeypatch.setattr(cli, "load", lambda args, facts=None: ctx)
    logged = []
    from wgt import system
    monkeypatch.setattr(system, "journal", lambda runner, tag, msg: logged.append((tag, msg)))
    assert cli.main(["watchdog", "exits"]) == 1
    assert ("wg-tools-exit-watchdog", "error: ValueError: bad wg output") in logged


@pytest.mark.parametrize("active,px,wx,started,wx_hs,port_tried,want", [
    (True, False, False, 0, 0, False, watchdog.UP),              # a client connects: bring it up
    (True, True, False, 0, 0, False, watchdog.UP),               # half up: bring it up properly
    (True, True, True, NOW - 5, 0, False, None),                 # just started: give WARP time
    (True, True, True, NOW - 999, NOW - 5, False, None),         # healthy
    (True, True, True, NOW - 999, NOW - 999, False, watchdog.NEW_PORT),   # WARP silent: new port first
    (True, True, True, NOW - 999, NOW - 999, True, watchdog.RESTART),     # still silent after that: restart
    (False, True, True, NOW - 999, NOW - 5, False, watchdog.DOWN),        # idle: take it down
    (False, False, True, 0, 0, False, watchdog.DOWN),            # idle and half up: still down
    (False, False, False, 0, 0, False, None),                    # idle and down: nothing to do
])
def test_decide(active, px, wx, started, wx_hs, port_tried, want):
    assert watchdog.decide(active, px, wx, NOW, started, wx_hs, port_tried) == want


def test_warp_off_jump_client_needs_only_the_jump_tunnel(make):
    """nyc-us.phone has WARP off: its jump comes up without the jump's WARP."""
    ctx = make({"ip link show dev px-par-fr": DOWN, "ip link show dev wx-par-fr": DOWN,
                "ip link show dev px-nyc-us": DOWN, "ip link show dev wx-nyc-us": DOWN})
    ctx.runner.answers["wg show wg0 latest-handshakes"] = (0, f"{pub(ctx, 'nyc-us.phone')}\t{NOW - 30}\n")
    watchdog.exits(ctx)
    assert "systemctl restart wg-quick@px-nyc-us" in ctx.runner.calls
    assert not any("wx-nyc-us" in c and c.startswith("systemctl") for c in ctx.runner.calls)


def test_warp_off_jump_with_dead_warp_left_alone(make):
    """Only WARP-off clients active, jump tunnel up, its WARP down: nothing to do."""
    ctx = make({"ip link show dev px-nyc-us": UP, "ip link show dev wx-nyc-us": DOWN,
                "ip link show dev px-par-fr": DOWN, "ip link show dev wx-par-fr": DOWN,
                "wg show px-nyc-us latest-handshakes": (0, f"K\t{NOW - 5}\n")})
    ctx.runner.answers["wg show wg0 latest-handshakes"] = (0, f"{pub(ctx, 'nyc-us.phone')}\t{NOW - 30}\n")
    watchdog.exits(ctx)
    assert not any(c.startswith("systemctl") for c in ctx.runner.calls)


def test_decide_without_wx():
    assert watchdog.decide(True, False, False, NOW, 0, 0, False, need_wx=False) == watchdog.UP
    assert watchdog.decide(True, True, False, NOW, 0, 0, False, need_wx=False) is None
    assert watchdog.decide(True, True, True, NOW, NOW - 999, 1, True, need_wx=False) is None


def test_probe_keeps_a_jump_tunnel_in_use(make, monkeypatch):
    """`jump test` on a jump whose tunnel carries WARP-off clients: it starts
    the jump's WARP for the check and stops only that again."""
    from wgt import exits
    monkeypatch.setattr(exits, "wait_hs", lambda ctx, e, seconds=10: None)
    ctx = make({"ip link show dev px-nyc-us": UP, "ip link show dev wx-nyc-us": DOWN})
    with exits.probing(ctx, ctx.snap.exit("nyc-us")):
        pass
    calls = ctx.runner.calls
    assert "systemctl stop wg-quick@wx-nyc-us" in calls
    assert not any(c.startswith("systemctl stop") and "px-nyc-us" in c for c in calls)


@pytest.mark.parametrize("need_wx,px_hs,wx_hs,port_tried,want", [
    (False, NOW - 999, 0, False, watchdog.RESTART_JUMP),      # WARP-off only: a dead jump is restarted
    (False, NOW - 5, 0, False, None),                         # WARP-off only, jump healthy
    (True, NOW - 999, NOW - 999, False, watchdog.RESTART_JUMP),   # jump dead: restart it, not just WARP
    (True, NOW - 5, NOW - 999, False, watchdog.NEW_PORT),     # jump fine, WARP silent: new port
    (True, NOW - 5, NOW - 999, True, watchdog.RESTART),       # ... then restart WARP alone
])
def test_decide_jump_health(need_wx, px_hs, wx_hs, port_tried, want):
    assert watchdog.decide(True, True, True, NOW, NOW - 999, wx_hs, port_tried, need_wx, px_hs) == want


def test_silent_warp_restarts_only_its_warp(make, tmp_path):
    """par-fr's WARP is silent after a new port but the jump tunnel is fine:
    only wx-par-fr restarts, so WARP-off clients on the jump keep their path."""
    ctx = make({"ip link show dev px-par-fr": UP, "ip link show dev wx-par-fr": UP,
                "ip link show dev px-nyc-us": DOWN, "ip link show dev wx-nyc-us": DOWN,
                "wg show px-par-fr latest-handshakes": (0, f"K\t{NOW - 5}\n"),
                "wg show wx-par-fr latest-handshakes": (0, f"K\t{NOW - 999}\n")})
    ctx.runner.answers["wg show wg0 latest-handshakes"] = (
        0, f"{pub(ctx, 'par-fr.mbp-1')}\t{NOW - 30}\n")
    Path(ctx.paths.exit_marker("par-fr")).write_text(str(NOW - 999))
    Path(ctx.paths.exit_port_marker("par-fr")).write_text("")
    watchdog.exits(ctx)
    assert "systemctl restart wg-quick@wx-par-fr" in ctx.runner.calls
    assert not any(c.startswith("systemctl restart") and "px-par-fr" in c for c in ctx.runner.calls)
