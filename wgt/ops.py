"""Operations shared by the commands: the lock, confirmations, the state
file, the endpoint, wg0.conf, and live peers."""

from __future__ import annotations

import fcntl
import os
import time
from typing import Optional

from . import live, render, store, ui
from .const import MARKER
from .errors import WgtError

def _open_lock(ctx) -> Optional[int]:
    try:
        return os.open(ctx.paths.lock_file, os.O_WRONLY | os.O_CREAT, 0o644)
    except OSError:
        if ctx.runner.dry_run:
            return None    # a dry run as a non-root user: nothing to serialize
        raise


def take_lock(ctx) -> None:
    """Serialize changing commands (and the exit watchdog), then re-read the
    server's files: decisions must use what's there once we hold the lock.
    Waits up to 15s (the exit watchdog holds it for a moment every 10s)."""
    if ctx.lock_fd is not None:
        return
    fd = _open_lock(ctx)
    if fd is None:
        return
    deadline = time.time() + 15
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.time() > deadline:
                os.close(fd)
                raise WgtError("another wireguard-tools command is running")
            time.sleep(0.2)
    ctx.lock_fd = fd
    ctx.reload()


def try_lock(ctx) -> bool:
    """Take the lock only if it's free right now (the watchdogs skip a run
    rather than wait), then re-read the server's files."""
    fd = _open_lock(ctx)
    if fd is None:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    ctx.lock_fd = fd
    ctx.reload()
    return True


def release_lock(ctx) -> None:
    if ctx.lock_fd is not None:
        fcntl.flock(ctx.lock_fd, fcntl.LOCK_UN)
        os.close(ctx.lock_fd)
        ctx.lock_fd = None


def confirm(ctx, question: str, default_yes: bool = False) -> None:
    """Ask before a change; --yes and --dry-run skip it. No means stop."""
    if ctx.runner.dry_run or ctx.args.yes:
        return
    if not ui.have_tty():
        raise WgtError(f'no terminal to confirm "{question}"; re-run with --yes')
    reply = ui.ask(f"    {question} {'[Y/n]' if default_yes else '[y/N]'} ")
    if not reply and default_yes:
        reply = "y"
    if not reply.lower().startswith("y"):
        raise WgtError(f"aborted at: {ctx.runner.current_step} (nothing further changed; --yes skips prompts)")


def require_setup(ctx) -> None:
    """The server must be set up; after rollback no WARP setting is saved."""
    if not ctx.cfg.saved.get("WARP"):
        raise WgtError("not set up; run 'wgt setup'")


def require_server(ctx) -> None:
    """The WireGuard server must be set up (clients need its keys and wg0.conf)."""
    pub = ctx.paths.server_pub
    if ctx.cfg.saved.get("WARP") and os.path.isfile(ctx.cfg.wg_conf) and os.path.isfile(pub) and os.path.getsize(pub) > 0:
        return
    if ctx.runner.dry_run:
        ctx.out.warn("server not set up yet; showing what would happen")
        return
    raise WgtError("no WireGuard server here yet; run 'wgt setup' first")


# --- state file ---

def state_set(ctx, key: str, value: str) -> None:
    if ctx.runner.dry_run:
        ctx.out.info(f"[dry-run] would record {key}={value} in {ctx.paths.state}")
        return
    _write_private(ctx.paths.state, store.state_set(store.read_text(ctx.paths.state), key, value))
    ctx.reload()


def state_unset(ctx, key: str) -> None:
    text = store.read_text(ctx.paths.state)
    if text is None:
        return
    if ctx.runner.dry_run:
        ctx.out.info(f"[dry-run] would clear {key} in {ctx.paths.state}")
        return
    _write_private(ctx.paths.state, store.state_unset(text, key))
    ctx.reload()


def _write_private(path: str, content: str) -> None:
    from .system import atomic_write
    atomic_write(path, content, 0o600)


# --- endpoint ---

def resolve_endpoint(ctx, save: bool) -> str:
    """Client endpoint: --endpoint / ENDPOINT, the saved value, or the detected
    public IPv4 (saved only when <save>, i.e. by setup)."""
    src = ctx.cfg.sources.get("ENDPOINT", "")
    ep = ctx.cfg.values.get("ENDPOINT", "") if src in ("flag", "env") else ""
    ep = ep or store.state_get(ctx.snap.state, "ENDPOINT")
    if not ep:
        ep = live.trace_field(live.trace(ctx.runner, "-4"), "ip")
        if not ep:
            raise WgtError("could not detect the public IP; pass --endpoint <ip-or-host>")
    if ":" in ep and not ep.startswith("["):
        ep = f"[{ep}]"
    ctx.cfg.values["ENDPOINT"] = ep
    if save and store.state_get(ctx.snap.state, "ENDPOINT") != ep:
        state_set(ctx, "ENDPOINT", ep)
    ctx.out.info(f"client endpoint: {ep}:{ctx.cfg.WG_PORT}")
    return ep


# --- wg0.conf and peers ---

def ensure_managed(ctx, path: str) -> None:
    """Refuse to overwrite a config these tools didn't create, unless --force."""
    text = store.read_text(path)
    if text is not None and MARKER not in text and not ctx.args.force:
        raise WgtError(f"{path} exists and was not created by wireguard-tools; move it aside or use --force")


def render_wg_conf(ctx) -> str:
    return render.render_wg_conf(ctx.cfg, ctx.snap, ctx.pub_if, store.psk_reader(ctx.paths))


def write_wg_conf(ctx) -> bool:
    ensure_managed(ctx, ctx.cfg.wg_conf)
    return ctx.files.write(ctx.cfg.wg_conf, 0o600, render_wg_conf(ctx))


def put_client(ctx, name: str, entry: Optional[store.Client] = None) -> None:
    ctx.files.write(ctx.paths.client_db, 0o600, store.put_client(store.read_text(ctx.paths.client_db), name, entry))
    ctx.reload()


def set_disabled(ctx, name: str, disabled: bool) -> None:
    ctx.files.write(ctx.paths.disabled_db, 0o600,
                    store.set_disabled(store.read_text(ctx.paths.disabled_db), name, disabled))
    ctx.reload()


def add_live_peer(ctx, name: str, pub: str, ip: str) -> None:
    ctx.runner.run(["wg", "set", ctx.cfg.WG_IF, "peer", pub, "preshared-key",
                    os.path.join(ctx.paths.psk_dir, f"{name}.psk"), "allowed-ips", ctx.cfg.client_addrs(ip)])
    ctx.out.info(f"peer live on {ctx.cfg.WG_IF} (other clients unaffected)")


def wg_up(ctx) -> bool:
    return live.if_exists(ctx.runner, ctx.cfg.WG_IF)
