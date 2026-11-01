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
    assert any("exit par-fr: a client is active; brought up" in c for c in calls)


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
                "wg show wx-par-fr latest-handshakes": (0, f"K\t{NOW - 900}\n")})
    ctx.runner.answers["wg show wg0 latest-handshakes"] = (0, f"{pub(ctx, 'par-fr.mbp-1')}\t{NOW - 10}\n")
    watchdog.exits(ctx)
    assert "wg set wx-par-fr listen-port 0" in ctx.runner.calls
    started = os.path.join(ctx.paths.run_dir, f"{EXIT_WATCHDOG}.par-fr")
    Path(started).write_text(str(NOW - 600))    # the new port didn't help for a while
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
