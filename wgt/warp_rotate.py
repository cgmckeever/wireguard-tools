"""`wgt warp rotate`: new WARP accounts, for this server's own WARP and its jumps'.
Apart from warp.py because it starts and probes tunnels, and exits imports warp."""

from __future__ import annotations

import os
import shutil
import time
from typing import TYPE_CHECKING, List, Optional

from . import exits, live, ops, results, store, units, warp
from .errors import WgtError

if TYPE_CHECKING:
    from .ctx import Ctx


def new_warp_account(ctx: Ctx, d: str) -> None:
    """Register a new WARP account for <d> (a wgcf directory): registered in a
    scratch directory first, so a refusal changes nothing; then its account and
    profile replace the old ones (which are backed up like any changed file)."""
    tmp = d.rstrip("/") + ".new"
    if not ctx.runner.dry_run:
        shutil.rmtree(tmp, ignore_errors=True)
    try:
        if not warp.register(ctx, tmp):
            raise WgtError("WARP refused a new registration; nothing changed (retry later)")
        if ctx.runner.dry_run:
            return
        for path in (ctx.paths.wgcf_account(d), ctx.paths.wgcf_profile(d)):
            fresh = store.read_text(os.path.join(tmp, os.path.basename(path)))
            if not fresh:
                raise WgtError(f"wgcf made no {os.path.basename(path)}; nothing changed")
            ctx.files.write(path, 0o600, fresh)
    finally:
        if not ctx.runner.dry_run:
            shutil.rmtree(tmp, ignore_errors=True)   # a refused registration's leftovers too


def rotate_host_warp(ctx: Ctx, res: results.Results) -> None:
    """The server's own WARP: a new account, warp.conf rewritten, warp restarted
    (warp.* configs pause for a few seconds), then traced."""
    ctx.out.log("New WARP account for this server")
    new_warp_account(ctx, ctx.paths.wgcf_dir)
    if ctx.runner.dry_run:
        ctx.out.info(f"[dry-run] would rewrite {ctx.cfg.warp_conf} and restart {ctx.cfg.WARP_IF}")
        return
    changed = ctx.files.write_config(ctx.cfg.warp_conf, 0o600, warp.render_profile(ctx, ctx.paths.wgcf_profile()))
    units.start_unit(ctx, ctx.cfg.WARP_IF, changed)
    for _ in range(15):   # a fresh account's first handshake
        if live.newest_hs(ctx.runner, ctx.cfg.WARP_IF) > 0:
            break
        time.sleep(1)
    exits.path_result(res, "host", True, live.trace_summary(live.trace(ctx.runner, "--interface", ctx.cfg.WARP_IF)))


def rotate_jump_warp(ctx: Ctx, res: results.Results, e: store.Exit) -> None:
    """A jump's WARP: a new account, wx-<code>.conf rewritten; the jump is taken
    down if up (the watchdog brings it back for its configs), then probed."""
    ctx.out.log(f"New WARP account for jump {e.code}")
    new_warp_account(ctx, ctx.paths.exit_wgcf_dir(e.code))
    if ctx.runner.dry_run:
        ctx.out.info(f"[dry-run] would rewrite {ctx.paths.wx_conf(e.code)} and restart the jump if it's up")
        return
    exits.write_confs(ctx, e)
    if exits.any_up(ctx, e):
        exits.down(ctx, e)
    try:
        with exits.probing(ctx, e):
            exits.path_result(res, ctx.snap.jump_label(e.code), True, exits.trace(ctx, e))
    except WgtError as err:
        res.result("FAIL", f"jump {e.code}: could not bring it up ({err})")


def cmd_warp_rotate(ctx: Ctx, words: List[str]) -> int:
    """wgt warp rotate [host|<jump>|all]: new WARP accounts (new keys, a new
    Cloudflare identity) for this server's own WARP (default), a jump's, or
    all of them. Device configs don't change."""
    if len(words) > 1:
        raise WgtError("usage: wgt warp rotate [host|<jump code>|all]")
    which = words[0] if words else "host"
    if which not in ("host", "all") and not ctx.snap.exit(which):
        raise WgtError(f"no jump '{which}' (host, all, or one of: "
                       f"{', '.join(e.code for e in ctx.snap.exits) or 'no jumps'})")
    jumps = ctx.snap.exits if which == "all" else [e for e in ctx.snap.exits if e.code == which]
    ops.take_lock(ctx)
    names = (["this server"] if which in ("host", "all") else []) + [f"jump {e.code}" for e in jumps]
    ctx.runner.step(f"New WARP accounts for: {', '.join(names)} (the old ones are backed up; "
                    "their warp. configs pause for a few seconds)", "restore the backed-up wgcf files, then wgt setup")
    ops.confirm(ctx, "Rotate WARP now?")
    if not warp.install_wgcf(ctx):
        raise WgtError("wgcf is needed to register WARP")
    res = results.Results(out=ctx.out.line)
    if which in ("host", "all"):
        rotate_host_warp(ctx, res)
    for e in jumps:
        rotate_jump_warp(ctx, res, e)
    return results.report(ctx, res)


def cmd_warp(ctx: Ctx, words: List[str]) -> Optional[int]:
    """wgt warp <command>: only rotate for now."""
    ops.require_setup(ctx)
    if not words:
        raise WgtError("usage: wgt warp rotate [host|<jump code>|all]")
    return ops.dispatch(ctx, "warp", words, {"rotate": cmd_warp_rotate})
