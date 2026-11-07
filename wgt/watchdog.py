"""The watchdogs, run by systemd timers as `wgt watchdog warp` (every 30s),
`wgt watchdog exits` and `wgt watchdog devices` (every 10s). Each run is quick and changes only
what it must; what it does is logged to the journal (`wgt log`)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, List, Optional

from . import devices, exits, live, ops, store
from .const import DEVICE_WATCHDOG, EXIT_HOLD, EXIT_IDLE, EXIT_WATCHDOG, WARP_WATCHDOG, WATCHDOG_STALE
from .errors import WgtError
from .system import journal, read_marker, remove_markers, write_marker

if TYPE_CHECKING:
    from .ctx import Ctx


def log_event(ctx: Ctx, tag: str, msg: str) -> None:
    """Log a watchdog's action to the journal under its own tag."""
    journal(ctx.runner, tag, msg)


def run_warp(ctx: Ctx) -> None:
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
    if r.attempt(["wg", "set", ifc, "listen-port", "0"]):
        log_event(ctx, WARP_WATCHDOG, f"{ifc}: no handshake for over {WATCHDOG_STALE}s; moved from port {old} to "
                              f"{live.listen_port(r, ifc)}")


def run_exits(ctx: Ctx) -> None:
    """A jump is up only while one of its clients has handshaked within
    EXIT_IDLE seconds: brought up when one does (its WARP too only if an
    active client has WARP on), taken down when none has. A jump tunnel silent
    for WATCHDOG_STALE seconds is restarted; a WARP inside a healthy one gets a
    new source port, then a restart of the WARP alone. Skips the run while
    wireguard-tools is changing things (its lock), and a jump that is being
    checked (its hold file)."""
    if not ctx.snap.exits or not ops.try_lock(ctx):
        return
    try:
        _exits_pass(ctx)
    finally:
        ops.release_lock(ctx)


# What the exit watchdog does to one jump on a run.
UP, NEW_PORT, RESTART, RESTART_JUMP, DOWN = "up", "new port", "restart", "restart jump", "down"


def decide(active: bool, px_up: bool, wx_up: bool, now: int, started_at: int, wx_last_hs: int,
           port_tried: bool, need_wx: bool = True, px_last_hs: Optional[int] = None) -> Optional[str]:
    """The exit watchdog's rule for one jump, from what it sees (None: leave it).

    - In use but not up (its WARP only counts if an active client needs it,
      <need_wx>: WARP-off clients use the jump tunnel alone): bring it up.
    - In use, up for over WATCHDOG_STALE, and the jump tunnel itself silent
      that long (<px_last_hs>; it keeps alive every 25s): restart the jump.
    - Its WARP needed but silent that long: first a new source port
      (Cloudflare sometimes drops one flow), then a restart of the WARP alone
      (the jump tunnel is fine, and may carry WARP-off clients).
    - Not in use but (partly) up: take it down."""
    if active:
        if not px_up or (need_wx and not wx_up):
            return UP
        settled = now - started_at > WATCHDOG_STALE
        if settled and px_last_hs is not None and now - px_last_hs > WATCHDOG_STALE:
            return RESTART_JUMP
        if not need_wx:
            return None
        if settled and now - wx_last_hs > WATCHDOG_STALE:
            return RESTART if port_tried else NEW_PORT
        return None
    return DOWN if px_up or wx_up else None


def _held(ctx: Ctx, code: str, now: int) -> bool:
    """`wgt jump add/test` is checking <code> (its hold file is under EXIT_HOLD old)."""
    try:
        return now - int(os.stat(ctx.paths.exit_hold(code)).st_mtime) < EXIT_HOLD
    except OSError:
        return False


def _exits_pass(ctx: Ctx) -> None:
    """One pass over the jumps. Uses r.attempt, not run: a timer job must get
    through every jump whatever fails, and logs what it did instead."""
    r = ctx.runner
    now = int(ctx.clock())
    if not live.if_exists(r, ctx.cfg.WG_IF):
        return   # no handshakes to read, not every client idle: wg0 may just be restarting
    hs = live.handshakes(r, ctx.cfg.WG_IF)
    for e in ctx.snap.exits:
        if _held(ctx, e.code, now):
            continue
        users = exits.active_clients(ctx.snap, e.code, hs, now)
        active = bool(users)
        need_wx = any(store.name_warp(c.name) for c in users)
        px_up, wx_up = live.if_exists(r, e.px), live.if_exists(r, e.wx)
        started_marker, port_marker = ctx.paths.exit_marker(e.code), ctx.paths.exit_port_marker(e.code)
        wx_last_hs = live.newest_hs(r, e.wx) if active and px_up and wx_up else 0
        px_last_hs = live.newest_hs(r, e.px) if active and px_up else None
        action = decide(active, px_up, wx_up, now, read_marker(started_marker), wx_last_hs, os.path.exists(port_marker),
                        need_wx, px_last_hs)
        if wx_last_hs and now - wx_last_hs <= WATCHDOG_STALE:
            remove_markers(port_marker)   # recovered: the next silent spell starts with a new port again
        _apply(ctx, e, action, _units_for(action, e, px_up, wx_up, need_wx), now)


def _units_for(action: Optional[str], e: store.Exit, px_up: bool, wx_up: bool, need_wx: bool) -> List[str]:
    """What <action> restarts. Only what must change: WARP-off clients ride the
    jump tunnel alone."""
    px_unit, wx_unit = e.units
    if action == UP:
        return [wx_unit] if px_up else ([px_unit, wx_unit] if need_wx else [px_unit])
    if action == RESTART_JUMP:
        return [px_unit, wx_unit] if need_wx or wx_up else [px_unit]
    return [wx_unit]


def _apply(ctx: Ctx, e: store.Exit, action: Optional[str], units: List[str], now: int) -> None:
    """Do what decide() chose for one jump, and log it."""
    r = ctx.runner
    started_marker, port_marker = ctx.paths.exit_marker(e.code), ctx.paths.exit_port_marker(e.code)
    stale = f"no WARP handshake for over {WATCHDOG_STALE}s"
    say = lambda msg: log_event(ctx, EXIT_WATCHDOG, f"jump {e.code}: {msg}")   # noqa: E731
    if action == UP:
        # restart, not start: a unit can be "active" with its link gone.
        if r.attempt(["systemctl", "restart", *units]):
            write_marker(started_marker, str(now))
            say("a client is active; brought up")
        else:
            say("a client is active, but bringing it up failed")
    elif action == NEW_PORT:
        if r.attempt(["wg", "set", e.wx, "listen-port", "0"]):
            write_marker(started_marker, str(now))
            write_marker(port_marker)
            say(f"{stale}; new source port")
    elif action in (RESTART, RESTART_JUMP):
        remove_markers(port_marker)
        if r.attempt(["systemctl", "restart", *units]):
            write_marker(started_marker, str(now))
            say(f"{stale} after a new port; its WARP restarted" if action == RESTART
                else f"jump tunnel silent for over {WATCHDOG_STALE}s; restarted")
    elif action == DOWN:
        idle = f"no client handshake in the last {EXIT_IDLE}s"
        say(f"{idle}; taken down" if exits.down(ctx, e, check=False) else f"{idle}, but taking it down failed")


def run_devices(ctx: Ctx) -> None:
    """Point each device's static address at the config it's using. Skips the
    run while wireguard-tools is changing things (its lock)."""
    if not ops.try_lock(ctx):
        return
    try:
        for msg in devices.sync(ctx):
            log_event(ctx, DEVICE_WATCHDOG, msg)
    finally:
        ops.release_lock(ctx)


def cmd_watchdog(ctx: Ctx, words: List[str]) -> None:
    """wgt watchdog warp|exits|devices: one pass, run by the systemd timers."""
    which = words[0] if words else ""
    if which == "warp":
        run_warp(ctx)
    elif which == "exits":
        run_exits(ctx)
    elif which == "devices":
        run_devices(ctx)
    else:
        raise WgtError("usage: wgt watchdog warp|exits|devices (run by the systemd timers)")
