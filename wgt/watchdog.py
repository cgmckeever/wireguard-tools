"""The two watchdogs, run by systemd timers as `wgt watchdog warp` (every 30s)
and `wgt watchdog exits` (every 10s). Each run is quick and changes only
what it must; what it does is logged to the journal (`wgt log`)."""

from __future__ import annotations

import os
import time
from typing import List

from . import live, ops
from .const import EXIT_IDLE, EXIT_WATCHDOG, WATCHDOG, WATCHDOG_STALE
from .errors import WgtError


def logger(ctx, tag: str, msg: str) -> None:
    ctx.runner.ok(["logger", "-t", tag, msg])


def _num(path: str) -> int:
    """A file's number (a timestamp), or 0 if it's missing or not a number."""
    try:
        with open(path) as f:
            v = f.read().strip()
        return int(v) if v.isdigit() else 0
    except OSError:
        return 0


def _touch(path: str, value: str = "") -> None:
    with open(path, "w") as f:
        f.write(value)


def _rm(*paths: str) -> None:
    for p in paths:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def warp(ctx) -> None:
    """If the WARP tunnel has had no handshake for WATCHDOG_STALE seconds on two
    runs in a row, give it a new source port: Cloudflare sometimes stops
    answering one flow (seen after reboots), and a new port gets a handshake
    within seconds. Nothing restarts."""
    r, ifc = ctx.runner, ctx.cfg.WARP_IF
    mark = os.path.join(ctx.paths.run_dir, WATCHDOG)
    if not live.if_exists(r, ifc):
        _rm(mark)
        return
    if live.hs_recent(r, ifc, WATCHDOG_STALE):
        _rm(mark)
        return
    # Unhealthy: act on the second run in a row, so a just-started tunnel gets time.
    if not os.path.exists(mark):
        _touch(mark)
        return
    old = live.listen_port(r, ifc)
    _rm(mark)
    if r.ok(["wg", "set", ifc, "listen-port", "0"]):
        logger(ctx, WATCHDOG, f"{ifc}: no handshake for over {WATCHDOG_STALE}s; moved from port {old} to "
                              f"{live.listen_port(r, ifc)}")


def exits(ctx) -> None:
    """An exit city is up only while one of its clients has handshaked within
    EXIT_IDLE seconds: brought up when one does, taken down when none has. An
    up exit whose WARP has had no handshake for WATCHDOG_STALE seconds gets a
    new source port, then a restart. Skips the run while wireguard-tools is
    changing things (its lock), and an exit that is being checked (its hold
    file)."""
    if not ctx.snap.exits or not ops.try_lock(ctx):
        return
    try:
        _exits(ctx)
    finally:
        ops.release_lock(ctx)


def _exits(ctx) -> None:
    r = ctx.runner
    now = int(time.time())
    hs = live.handshakes(r, ctx.cfg.WG_IF)

    def has(ifc: str) -> bool:
        return live.if_exists(r, ifc)

    for e in ctx.snap.exits:
        code, run = e.code, ctx.paths.run_dir
        hold = os.path.join(run, f"{EXIT_WATCHDOG}.{code}.hold")
        try:
            if now - int(os.stat(hold).st_mtime) < 60:
                continue
        except OSError:
            pass
        active = any(0 < hs.get(c.pub, 0) and now - hs[c.pub] <= EXIT_IDLE
                     for c in ctx.snap.exit_clients(code) if not ctx.snap.is_disabled(c.name))
        started = os.path.join(run, f"{EXIT_WATCHDOG}.{code}")
        units: List[str] = [f"wg-quick@px-{code}", f"wg-quick@wx-{code}"]
        if active:
            if not (has(f"px-{code}") and has(f"wx-{code}")):
                # restart, not start: a unit can be "active" with its link gone.
                if r.ok(["systemctl", "restart"] + units):
                    _touch(started, str(now))
                    logger(ctx, EXIT_WATCHDOG, f"exit {code}: a client is active; brought up")
                else:
                    logger(ctx, EXIT_WATCHDOG, f"exit {code}: a client is active, but bringing it up failed")
            elif now - _num(started) > WATCHDOG_STALE and now - live.newest_hs(r, f"wx-{code}") > WATCHDOG_STALE:
                if os.path.exists(started + ".port"):
                    _rm(started + ".port")
                    if r.ok(["systemctl", "restart"] + units):
                        _touch(started, str(now))
                        logger(ctx, EXIT_WATCHDOG, f"exit {code}: no WARP handshake for over {WATCHDOG_STALE}s "
                                                   "after a new port; restarted")
                elif r.ok(["wg", "set", f"wx-{code}", "listen-port", "0"]):
                    _touch(started, str(now))
                    _touch(started + ".port")
                    logger(ctx, EXIT_WATCHDOG, f"exit {code}: no WARP handshake for over {WATCHDOG_STALE}s; "
                                               "new source port")
        elif has(f"px-{code}") or has(f"wx-{code}"):
            r.ok(["systemctl", "stop"] + list(reversed(units)))
            for ifc in (f"wx-{code}", f"px-{code}"):
                if has(ifc):
                    r.ok(["ip", "link", "del", "dev", ifc])
            _rm(started, started + ".port")
            if has(f"px-{code}") or has(f"wx-{code}"):
                logger(ctx, EXIT_WATCHDOG, f"exit {code}: no client handshake in the last {EXIT_IDLE}s, "
                                           "but taking it down failed")
            else:
                logger(ctx, EXIT_WATCHDOG, f"exit {code}: no client handshake in the last {EXIT_IDLE}s; taken down")


def cmd_watchdog(ctx, words: List[str]) -> None:
    which = words[0] if words else ""
    if which == "warp":
        warp(ctx)
    elif which == "exits":
        exits(ctx)
    else:
        raise WgtError("usage: wgt watchdog warp|exits (run by the systemd timers)")
