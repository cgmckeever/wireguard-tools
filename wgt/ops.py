"""Operations shared by the commands: the lock, confirmations, setup checks,
the state file, the endpoint, wg0.conf, live peers, the client and exit
databases, and subcommand dispatch (verbs, leftover words)."""

from __future__ import annotations

import fcntl
import os
import time
from typing import TYPE_CHECKING, Callable, Dict, List, Mapping, Optional, Tuple

from . import live, render, store, ui
from .const import MARKER
from .errors import WgtError
from .system import write_private

if TYPE_CHECKING:
    from .ctx import Ctx


def _open_lock(ctx: Ctx) -> Optional[int]:
    """The lock file's descriptor (None for a dry run that can't open it)."""
    try:
        return os.open(ctx.paths.lock_file, os.O_WRONLY | os.O_CREAT, 0o644)
    except OSError:
        if ctx.runner.dry_run:
            return None    # a dry run as a non-root user: nothing to serialize
        raise


def take_lock(ctx: Ctx) -> None:
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
                raise WgtError("another wireguard-tools command is running") from None
            time.sleep(0.2)
    ctx.lock_fd = fd
    ctx.reload()


def try_lock(ctx: Ctx) -> bool:
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


def release_lock(ctx: Ctx) -> None:
    """Let other wgt runs (and the watchdogs) in again."""
    if ctx.lock_fd is not None:
        fcntl.flock(ctx.lock_fd, fcntl.LOCK_UN)
        os.close(ctx.lock_fd)
        ctx.lock_fd = None


def confirm(ctx: Ctx, question: str, default_yes: bool = False) -> None:
    """Ask before a change; --yes and --dry-run skip it. No means stop."""
    if ctx.runner.dry_run or ctx.args.yes:
        return
    if not ui.have_tty():
        raise WgtError(f'no terminal to confirm "{question}"; re-run with --yes')
    if not ui.yes(question, default_yes):
        raise WgtError(f"aborted at: {ctx.runner.current_step} (nothing further changed; --yes skips prompts)")


def is_set_up(ctx: Ctx) -> bool:
    """`wgt setup` has run here (its settings are saved; rollback clears WARP)."""
    return bool(ctx.cfg.saved.get("WARP"))


def require_setup(ctx: Ctx) -> None:
    """For commands that need only `wgt setup` to have run (jumps): its
    settings are saved (after rollback no WARP setting is)."""
    if not is_set_up(ctx):
        raise WgtError("not set up; run 'wgt setup'")


def require_server(ctx: Ctx) -> None:
    """For commands that write client configs: setup ran and the server's keys
    and wg0.conf exist. A dry run only warns, to show what would happen."""
    pub = ctx.paths.server_pub
    have_pub = os.path.isfile(pub) and os.path.getsize(pub) > 0
    if is_set_up(ctx) and os.path.isfile(ctx.cfg.wg_conf) and have_pub:
        return
    if ctx.runner.dry_run:
        ctx.out.warn("server not set up yet; showing what would happen")
        return
    raise WgtError("no WireGuard server here yet; run 'wgt setup' first")


# --- state file ---

def state_set(ctx: Ctx, key: str, value: str) -> None:
    """Record <key>=<value> in wg-tools.state (dry-run aware)."""
    if ctx.runner.dry_run:
        ctx.out.info(f"[dry-run] would record {key}={value} in {ctx.paths.state}")
        return
    write_private(ctx.paths.state, store.state_set(store.read_text(ctx.paths.state), key, value))
    ctx.reload()


def state_unset(ctx: Ctx, key: str) -> None:
    """Drop <key> from wg-tools.state (dry-run aware)."""
    text = store.read_text(ctx.paths.state)
    if text is None:
        return
    if ctx.runner.dry_run:
        ctx.out.info(f"[dry-run] would clear {key} in {ctx.paths.state}")
        return
    write_private(ctx.paths.state, store.state_unset(text, key))
    ctx.reload()


def default_out_if(ctx: Ctx, warp: Optional[str] = None) -> str:
    """Where clients without a jump leave: warp, or the public interface
    with WARP off. <warp> ("on"/"off") asks about a mode other than the current one."""
    on = ctx.cfg.warp_on if warp is None else warp == "on"
    return ctx.cfg.WARP_IF if on else ctx.pub_if


# --- endpoint ---

def resolve_endpoint(ctx: Ctx, save: bool) -> str:
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

def ensure_managed(ctx: Ctx, path: str) -> None:
    """Refuse to overwrite a config these tools didn't create, unless --force."""
    text = store.read_text(path)
    if text is not None and MARKER not in text and not ctx.args.force:
        raise WgtError(f"{path} exists and was not created by wireguard-tools; move it aside or use --force")


def render_wg_conf(ctx: Ctx) -> str:
    return render.render_wg_conf(ctx.cfg, ctx.snap, ctx.pub_if, store.psk_reader(ctx.paths, ctx.files.read))


def write_wg_conf(ctx: Ctx) -> bool:
    """Write wg0.conf; True if its settings changed (not just comments)."""
    ensure_managed(ctx, ctx.cfg.wg_conf)
    return ctx.files.write_config(ctx.cfg.wg_conf, 0o600, render_wg_conf(ctx))


# The database writes below read through ctx.files, so a dry run's second
# change to the same file builds on its first (rename = add new + drop old),
# and each reloads ctx.snap: snapshot values read before them are stale after.

def save_client(ctx: Ctx, entry: store.Client) -> None:
    """Add <entry> to clients.db, replacing any client of that name; reloads ctx.snap."""
    _write_clients(ctx, entry.name, entry)


def remove_client(ctx: Ctx, name: str) -> None:
    """Drop <name> from clients.db; reloads ctx.snap."""
    _write_clients(ctx, name, None)


def _write_clients(ctx: Ctx, name: str, entry: Optional[store.Client]) -> None:
    db = ctx.paths.client_db
    ctx.files.write(db, 0o600, store.put_client(ctx.files.read(db), name, entry))
    ctx.reload()


def set_disabled(ctx: Ctx, name: str, disabled: bool) -> None:
    """Mark <name> disabled (or not) in clients.disabled; reloads ctx.snap."""
    db = ctx.paths.disabled_db
    ctx.files.write(db, 0o600, store.set_disabled(ctx.files.read(db), name, disabled))
    ctx.reload()


def save_exit(ctx: Ctx, entry: store.Exit) -> None:
    """Add <entry> to exits.db, replacing any exit of that code; reloads ctx.snap."""
    _write_exits(ctx, entry.code, entry)


def remove_exit(ctx: Ctx, code: str) -> None:
    """Drop <code> from exits.db; reloads ctx.snap."""
    _write_exits(ctx, code, None)


def _write_exits(ctx: Ctx, code: str, entry: Optional[store.Exit]) -> None:
    db = ctx.paths.exit_db
    ctx.files.write(db, 0o600, store.put_exit(ctx.files.read(db), code, entry))
    ctx.reload()


def set_device(ctx: Ctx, name: str, number: Optional[int], groups: Optional[Tuple[str, ...]] = None) -> None:
    """Give device <name> a static <number>, or none, and <groups> (None: the
    ones it has); reloads ctx.snap."""
    db = ctx.paths.device_db
    ctx.files.write(db, 0o600, store.put_device(ctx.files.read(db), name, number, groups))
    ctx.reload()


# --- command words ---

Command = Callable[..., Optional[int]]   # (ctx, words) -> exit status or None


def dispatch(ctx: Ctx, noun: str, words: List[str], table: Mapping[str, Command],
             aliases: Optional[Dict[str, str]] = None) -> Optional[int]:
    """wgt <noun> <verb> ...: run the verb's command (no verb: list). An
    unknown verb's error lists the verbs from <table>."""
    verb, rest = (words[0], words[1:]) if words else ("list", [])
    verb = (aliases or {}).get(verb, verb)
    fn = table.get(verb)
    if fn is None:
        raise WgtError(f"unknown {noun} command: {verb} ({', '.join(table)}; see: wgt help)")
    return fn(ctx, rest)


def no_more_words(extra: List[str], usage: str) -> None:
    """Refuse leftover words (`wgt client remove a b` must not remove only a)."""
    if extra:
        raise WgtError(f"unexpected: {' '.join(extra)} (usage: {usage})")


def add_live_peer(ctx: Ctx, name: str, pub: str, ip: str) -> None:
    ctx.runner.run(["wg", "set", ctx.cfg.WG_IF, "peer", pub, "preshared-key",
                    ctx.paths.psk(name), "allowed-ips", ctx.cfg.client_addrs(ip)])
    ctx.out.info(f"peer live on {ctx.cfg.WG_IF} (other clients unaffected)")


def wg_up(ctx: Ctx) -> bool:
    return live.if_exists(ctx.runner, ctx.cfg.WG_IF)
