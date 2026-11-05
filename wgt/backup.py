"""wgt backup and wgt restore: one archive of the server's keys, clients,
jumps and settings, and setting a new server up from it."""

from __future__ import annotations

import glob
import os
import re
import tarfile
import time
from typing import TYPE_CHECKING, List, Optional

from . import clients, ops, store, units
from .const import HOST_STATE_KEYS, KEEP_ARCHIVES, Paths
from .ctx import VALUE_FLAGS
from .errors import WgtError
from .system import write_private

if TYPE_CHECKING:
    from .ctx import Ctx


def check_archive(archive: str, paths: Paths) -> List[str]:
    """The archive's members as absolute paths, after checking it is safe to
    extract: it is extracted as root with absolute paths, so anything that
    could land outside the WireGuard directory (.., links, devices) is refused,
    and it must be a wireguard-tools backup (wg-tools.state inside)."""
    try:
        with tarfile.open(archive, "r:gz") as t:
            infos = t.getmembers()
    except (tarfile.TarError, OSError):
        raise WgtError(f"{archive} is not a readable .tar.gz") from None
    members = [m.name if m.name.startswith("/") else "/" + m.name for m in infos]
    for m, name in zip(infos, members):
        if ".." in name.split("/") or m.issym() or m.islnk() or not (m.isfile() or m.isdir()):
            raise WgtError(f"{archive}: refusing an unsafe entry: {m.name}")
    if paths.state not in members:
        raise WgtError(f"{archive} is not a wireguard-tools backup (no {paths.state} inside)")
    return members


def rebuild_backup_map(ctx: Ctx, keep_map: Optional[str], members: List[str]) -> None:
    """After extracting: keep this server's own backup record (not the
    archive's), and record every restored file as NEW, or its first change
    would keep the restored copy as the file's "original" for rollback."""
    if keep_map is not None:
        write_private(ctx.paths.backup_map, keep_map)
    elif os.path.exists(ctx.paths.backup_map):
        os.remove(ctx.paths.backup_map)
    backup_map = store.read_text(ctx.paths.backup_map) or ""
    for name in members:
        if name.startswith(ctx.paths.wg_dir + "/") and not name.endswith("/") \
                and name != ctx.paths.backup_map and os.path.isfile(name) and not store.backup_orig(backup_map, name):
            backup_map += f"{name}\tNEW\n"
    write_private(ctx.paths.backup_map, backup_map)


def cmd_backup(ctx: Ctx, words: List[str]) -> None:
    """Archive keys, configs, clients, jumps and state (mode 600: it holds
    private keys), then prune old archives and change copies."""
    ops.no_more_words(words[1:], "wgt backup [dir]")
    dest_dir = words[0] if words else ctx.paths.backup_dir
    sources = [p for p in (ctx.paths.wg_dir, ctx.paths.sysctl_file, ctx.cfg.dropin_dir) if os.path.exists(p)]
    if not sources:
        raise WgtError("nothing to back up")
    ops.take_lock(ctx)   # don't archive mid-change
    short_host = ctx.facts.hostname.split(".")[0]
    archive = os.path.join(dest_dir, f"wireguard-tools.{short_host}.{time.strftime('%Y%m%d-%H%M%S')}.tar.gz")
    ctx.runner.step(f"Back up {' '.join(sources)} to {archive}", f"rm {archive}")
    if not ctx.runner.dry_run:
        os.makedirs(dest_dir, mode=0o700, exist_ok=True)
        old_umask = os.umask(0o077)
        try:
            ctx.runner.run(["tar", "-czf", archive, "-P", *sources])
        finally:
            os.umask(old_umask)
        os.chmod(archive, 0o600)
        archives = sorted(glob.glob(os.path.join(dest_dir, "wireguard-tools.*.tar.gz")))
        for stale in archives[: max(0, len(archives) - KEEP_ARCHIVES)]:
            os.remove(stale)
            ctx.out.info(f"pruned old archive: {stale}")
        # Archived, so each file's older change copies can go (originals stay).
        for line in (store.read_text(ctx.paths.backup_map) or "").splitlines():
            ctx.files.prune(line.split("\t")[0])
    ctx.out.warn("the archive contains private keys; keep it somewhere safe")
    ctx.out.info("restore on a fresh server: wgt restore <archive>")
    clients.fetch_hint(ctx, archive)


def cmd_restore(ctx: Ctx, words: List[str]) -> None:
    """Set this server up from a backup: keys, clients, WARP accounts, jumps
    and settings come across; the old server's baseline, firewall and
    backup records are dropped and an IP endpoint is re-detected; then setup."""
    if not words:
        raise WgtError("usage: wgt restore <archive.tar.gz>")
    ops.no_more_words(words[1:], "wgt restore <archive.tar.gz>")
    archive = os.path.realpath(words[0])
    if not os.path.isfile(archive):
        raise WgtError(f"no such file: {words[0]}")
    members = check_archive(archive, ctx.paths)
    if ops.is_set_up(ctx):
        raise WgtError("this server is already set up; run 'wgt rollback' (or 'rollback --purge') first")
    ops.take_lock(ctx)
    has_warp = ctx.paths.wgcf_account() in members
    has_exits = any(re.match(re.escape(ctx.paths.exit_dir) + r"/.*/wgcf-account\.toml$", m) for m in members)
    what = "keys, clients, settings"
    what += " and WARP account" if has_warp else ""
    what += ", jumps" if has_exits else ""
    ctx.runner.step(f"Restore {what} from {archive}", "wgt rollback --purge")
    ops.confirm(ctx, "Restore from this backup?")
    keep_map = store.read_text(ctx.paths.backup_map)
    ctx.runner.run(["tar", "-xzpf", archive, "-P", ctx.paths.wg_dir])
    if ctx.runner.dry_run:
        ctx.out.info("[dry-run] would then run: wgt setup")
        return
    ctx.reload()
    # The old server's host facts, firewall note and detected IP don't apply here.
    for k in HOST_STATE_KEYS:
        ops.state_unset(ctx, k)
    if re.match(r"^\[?[0-9a-fA-F:.]+\]?$", store.state_get(ctx.snap.state, "ENDPOINT")):
        ops.state_unset(ctx, "ENDPOINT")
    rebuild_backup_map(ctx, keep_map, members)
    ctx.out.info("restored; clients: " + " ".join(c.name for c in store.load(ctx.paths).clients))
    if has_warp:
        ctx.out.warn("the WARP account is now on this server too; don't keep running it on the old one")
    if has_exits:
        ctx.out.warn("the jumps' keys and WARP accounts are now here too; remove them from the old server")
    ops.release_lock(ctx)
    ctx.out.log("Setting up")
    ctx.out.info("if setup stops, fix the cause and run 'wgt setup' (the restored files stay in place)")
    cmd = [units.wgt_command(ctx)] + (["--yes"] if ctx.args.yes else []) + ["setup"] + setup_flags(ctx)
    os.execv(cmd[0], cmd)


def setup_flags(ctx: Ctx) -> List[str]:
    """The setting flags given to restore (--endpoint, --port, --location...),
    passed on to the setup it runs, so a restore can change them."""
    flags: List[str] = []
    for flag, key in VALUE_FLAGS.items():
        if key in ctx.args.settings:
            flags += [flag, ctx.args.settings[key]]
    if ctx.args.location:
        flags += ["--location", ctx.args.location]
    return flags
