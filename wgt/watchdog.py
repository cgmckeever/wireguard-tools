"""The two watchdogs, run by systemd timers as `wgt watchdog warp` (every 30s)
and `wgt watchdog exits` (every 10s). Each run is quick and changes only
what it must; what it does is logged to the journal (`wgt log`)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, List, Optional

from . import exits as jumps
from . import live, ops
from .const import EXIT_HOLD, EXIT_IDLE, EXIT_WATCHDOG, WATCHDOG, WATCHDOG_STALE
from .errors import WgtError
from .system import journal, read_marker, remove_markers, write_marker

if TYPE_CHECKING:
    from .ctx import Ctx


def log_event(ctx: Ctx, tag: str, msg: str) -> None:
    """Log a watchdog's action to the journal under its own tag."""
    journal(ctx.runner, tag, msg)


def warp(ctx: Ctx) -> None:
    """If the WARP tunnel has had no handshake for WATCHDOG_STALE seconds on two
    runs in a row, give it a new source port: Cloudflare sometimes stops
    answering one flow (seen after reboots), and a new port gets a handshake
    within seconds. Nothing restarts."""
    r, ifc = ctx.runner, ctx.cfg.WARP_IF
    marker = ctx.paths.warp_marker
    if not live.if_exists(r, ifc):
        remove_markers(marker)
        return
    if live.hs_recent(r, ifc, WATCHDOG_STALE, ctx.clock()):
        remove_markers(marker)
        return
    # Unhealthy: act on the second run in a row, so a just-started tunnel gets time.
    if not os.path.exists(marker):
        write_marker(marker)
        return
    old = live.listen_port(r, ifc)
    remove_markers(marker)
    if r.ok(["wg", "set", ifc, "listen-port", "0"]):
        log_event(ctx, WATCHDOG, f"{ifc}: no handshake for over {WATCHDOG_STALE}s; moved from port {old} to "
                              f"{live.listen_port(r, ifc)}")


def exits(ctx: Ctx) -> None:
    """A jump is up only while one of its clients has handshaked within
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


# What the exit watchdog does to one jump on a run.
UP, NEW_PORT, RESTART, DOWN = "up", "new port", "restart", "down"


def decide(active: bool, px_up: bool, wx_up: bool, now: int, started_at: int, wx_last_hs: int,
           port_tried: bool, need_wx: bool = True) -> Optional[str]:
    """The exit watchdog's rule for one jump, from what it sees (None: leave it).

    - In use but not up (its WARP only counts if an active client needs it,
      <need_wx>: WARP-off clients use the jump tunnel alone): bring it up.
    - In use, up for over WATCHDOG_STALE, and its WARP needed but silent that
      long: first a new source port (Cloudflare sometimes drops one flow), then
      a restart.
    - Not in use but (partly) up: take it down."""
    if active:
        if not px_up or (need_wx and not wx_up):
            return UP
        if not need_wx:
            return None
        if now - started_at > WATCHDOG_STALE and now - wx_last_hs > WATCHDOG_STALE:
            return RESTART if port_tried else NEW_PORT
        return None
    return DOWN if px_up or wx_up else None


def _held(ctx: Ctx, code: str, now: int) -> bool:
    """`wgt jump add/test` is checking <code> (its hold file is under EXIT_HOLD old)."""
    try:
        return now - int(os.stat(ctx.paths.exit_hold(code)).st_mtime) < EXIT_HOLD
    except OSError:
        return False


def _exits(ctx: Ctx) -> None:
    """One pass over the jumps. Uses r.ok, not run: a timer job must get
    through every jump whatever fails, and logs what it did instead."""
    r = ctx.runner
    now = int(ctx.clock())
    hs = live.handshakes(r, ctx.cfg.WG_IF)
    for e in ctx.snap.exits:
        if _held(ctx, e.code, now):
            continue
        users = jumps.active_clients(ctx.snap, e.code, hs, now)
        active = bool(users)
        need_wx = any(not ctx.snap.warp_off(c.name) for c in users)
        # Touch the jump tunnel only when it must: WARP-off clients ride it alone.
        px_up = live.if_exists(r, e.px)
        direct_live = px_up and any(ctx.snap.warp_off(c.name) for c in users)
        wx_up = live.if_exists(r, e.wx)
        started_marker, port_marker = ctx.paths.exit_marker(e.code), ctx.paths.exit_port_marker(e.code)
        wx_last_hs = live.newest_hs(r, e.wx) if active and px_up and wx_up else 0
        action = decide(active, px_up, wx_up, now, read_marker(started_marker), wx_last_hs, os.path.exists(port_marker),
                        need_wx)
        stale = f"no WARP handshake for over {WATCHDOG_STALE}s"
        units = [e.units[1]] if direct_live else (e.units if need_wx else [e.units[0]])
        if action == UP:
            # restart, not start: a unit can be "active" with its link gone.
            if r.ok(["systemctl", "restart", *units]):
                write_marker(started_marker, str(now))
                log_event(ctx, EXIT_WATCHDOG, f"jump {e.code}: a client is active; brought up")
            else:
                log_event(ctx, EXIT_WATCHDOG, f"jump {e.code}: a client is active, but bringing it up failed")
        elif action == NEW_PORT:
            if r.ok(["wg", "set", e.wx, "listen-port", "0"]):
                write_marker(started_marker, str(now))
                write_marker(port_marker)
                log_event(ctx, EXIT_WATCHDOG, f"jump {e.code}: {stale}; new source port")
        elif action == RESTART:
            remove_markers(port_marker)
            if r.ok(["systemctl", "restart", *units]):
                write_marker(started_marker, str(now))
                log_event(ctx, EXIT_WATCHDOG, f"jump {e.code}: {stale} after a new port; restarted")
        elif action == DOWN:
            idle = f"no client handshake in the last {EXIT_IDLE}s"
            if jumps.down(ctx, e, check=False):
                log_event(ctx, EXIT_WATCHDOG, f"jump {e.code}: {idle}; taken down")
            else:
                log_event(ctx, EXIT_WATCHDOG, f"jump {e.code}: {idle}, but taking it down failed")


def cmd_watchdog(ctx: Ctx, words: List[str]) -> None:
    """wgt watchdog warp|exits: one pass, run by the systemd timers."""
    which = words[0] if words else ""
    if which == "warp":
        warp(ctx)
    elif which == "exits":
        exits(ctx)
    else:
        raise WgtError("usage: wgt watchdog warp|exits (run by the systemd timers)")
