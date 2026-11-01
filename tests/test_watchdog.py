"""The exit and WARP watchdogs' decisions, with a fake runner standing in for
wg, ip and systemctl."""

from __future__ import annotations

import os
import shutil
import time

import pytest
from conftest import FIXTURES

from wgt import cli, watchdog
from wgt.const import EXIT_WATCHDOG, WATCHDOG, Paths
from wgt.host import Facts
from wgt.system import FakeRunner, Out

NOW = int(time.time())


@pytest.fixture
def make(tmp_path):
    def build(answers):
        wg = tmp_path / "wg"
        if not wg.exists():
            shutil.copytree(os.path.join(FIXTURES, "exits", "wg"), wg)
        run = tmp_path / "run"
        run.mkdir(exist_ok=True)
        paths = Paths(wg_dir=str(wg), lock_file=str(tmp_path / "lock"), run_dir=str(run))
        r = FakeRunner(answers, out=Out())
        facts = Facts(kernel_ipv6=True, has_ipv6=False, hostname="", routes4="", default_route="")
        return cli.load(cli.parse([]), paths=paths, runner=r, env={}, facts=facts)
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
    open(started, "w").write(str(NOW - 600))    # the new port didn't help for a while
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
