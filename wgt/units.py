"""systemd units: wg-quick interfaces, the watchdog timers, the firewall (ufw)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, List, Optional

from . import live, render, store
from .const import EXIT_IDLE, EXIT_WATCHDOG, WATCHDOG, WATCHDOG_STALE

if TYPE_CHECKING:
    from .cli import Ctx


def checkout_wgt() -> str:
    """This checkout's bin/wgt."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "wgt")


def wgt_command(ctx: Optional[Ctx] = None) -> str:
    """How systemd runs this tool: the <sbin>/wgt link setup makes, or this
    checkout's bin/wgt."""
    link = os.path.join(ctx.paths.sbin_dir if ctx else "/usr/local/sbin", "wgt")
    return link if os.path.exists(link) else checkout_wgt()


def write_timer(ctx: Ctx, name: str, what: str, service: str, timer: str) -> None:
    """Install a watchdog's service and timer and keep the timer enabled and on.
    Any old standalone script of the same name (bash version) is removed."""
    changed = False
    for path, content in ((ctx.paths.unit_file(f"{name}.service"), service),
                          (ctx.paths.unit_file(f"{name}.timer"), timer)):
        changed |= ctx.files.write(path, 0o644, content)
    old_script = os.path.join(ctx.paths.sbin_dir, name)
    if os.path.exists(old_script):
        ctx.files.forget(old_script)
    if changed:
        ctx.runner.run(["systemctl", "daemon-reload"])
    if not (live.unit_active(ctx.runner, f"{name}.timer") and live.unit_enabled(ctx.runner, f"{name}.timer")):
        ctx.runner.step(f"Start the {what}", f"systemctl disable --now {name}.timer")
        ctx.runner.run(["systemctl", "enable", "--now", f"{name}.timer"])


def remove_timer(ctx: Ctx, name: str) -> None:
    """Stop a watchdog's timer and remove (or restore) its files."""
    ctx.runner.run(["systemctl", "disable", "--now", f"{name}.timer"], check=False)
    for path in (ctx.paths.unit_file(f"{name}.timer"), ctx.paths.unit_file(f"{name}.service"),
                 os.path.join(ctx.paths.sbin_dir, name)):
        ctx.files.restore_original(path, remove_new=True)


def write_watchdog(ctx: Ctx) -> None:
    """The WARP watchdog: every 30s, `wgt watchdog warp`."""
    svc = render.render_template("watchdog.service.tmpl", NAME=WATCHDOG,
                                 WHAT=f"WARP watchdog (new source port if {ctx.cfg.WARP_IF} stops handshaking)",
                                 COMMAND=f"{wgt_command(ctx)} watchdog warp", TIMEOUT="60")
    timer = render.render_template("warp-watchdog.timer.tmpl")
    write_timer(ctx, WATCHDOG,
                f"WARP watchdog ({WATCHDOG}.timer: new source port for {ctx.cfg.WARP_IF} after "
                f"{WATCHDOG_STALE}s without a handshake)", svc, timer)


def write_exit_watchdog(ctx: Ctx) -> None:
    """The exit watchdog: every 10s, `wgt watchdog exits`."""
    svc = render.render_template("watchdog.service.tmpl", NAME=EXIT_WATCHDOG,
                                 WHAT="exit-city watchdog (exits up while their clients are active)",
                                 COMMAND=f"{wgt_command(ctx)} watchdog exits", TIMEOUT="30")
    timer = render.render_template("exit-watchdog.timer.tmpl")
    write_timer(ctx, EXIT_WATCHDOG,
                f"exit watchdog ({EXIT_WATCHDOG}.timer: exits up while their clients are active, "
                f"down after {EXIT_IDLE}s idle)", svc, timer)


def open_firewall(ctx: Ctx, outs: List[str]) -> None:
    """ufw (if active): allow the WireGuard port and wg0 -> every way out."""
    st = ctx.runner.query(["ufw", "status"])
    if "Status: active" not in st:
        ctx.out.info(f"ufw not active; {ctx.cfg.WG_IF}'s PostUp opens udp/{ctx.cfg.WG_PORT} in iptables INPUT")
        return
    wg = ctx.cfg.WG_IF
    ctx.runner.step(f"Open ufw: allow {ctx.cfg.WG_PORT}/udp and routing {wg} -> {' '.join(outs)}",
                    f"ufw delete allow {ctx.cfg.WG_PORT}/udp; ufw route delete allow in on {wg} out on <each>")
    ctx.runner.run(["ufw", "allow", f"{ctx.cfg.WG_PORT}/udp", "comment", "wireguard-tools"])
    for o in outs:
        ctx.runner.run(["ufw", "route", "allow", "in", "on", wg, "out", "on", o, "comment", "wireguard-tools"])
    if store.state_get(ctx.snap.state, "UFW") != "1":
        from .ops import state_set
        state_set(ctx, "UFW", "1")

