"""Operations shared by the commands: the lock, confirmations, setup checks,
the state file, the endpoint, wg0.conf, live peers, the client and exit
databases, and subcommand dispatch (verbs, leftover words)."""

from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import time
from typing import TYPE_CHECKING, Callable, Iterator, List, Mapping, Optional, Tuple

from . import config, live, render, store, ui
from .const import MARKER, SET_UP_KEYS, SETTING_KEYS
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
    """Serialize changing commands (and the jump and device watchdogs), then re-read
    the server's files: decisions must use what's there once we hold the lock.
    Waits up to 15s (each watchdog holds it for a moment every 10s). A watchdog skips
    its run while a command holds it, so a question that can come first does
    (lock_unless_asking, confirm_then_lock)."""
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
    if settings_moved(ctx):
        release_lock(ctx)
        raise WgtError("the server's settings changed while this waited for another wireguard-tools "
                       "command (setup); run it again")


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
    if settings_moved(ctx):   # the next run loads the new ones
        release_lock(ctx)
        return False
    return True


def settings_moved(ctx: Ctx) -> bool:
    """The saved settings aren't the ones ctx.cfg was resolved from: they're read at
    load, before the lock, and a setup that held it meanwhile may have changed them.
    Carrying on would write the old ones into wg0.conf and new configs."""
    return store.saved_settings(ctx.snap.state, SETTING_KEYS) != ctx.cfg.saved


def release_lock(ctx: Ctx) -> None:
    """Let other wgt runs (and the watchdogs) in again."""
    if ctx.lock_fd is not None:
        fcntl.flock(ctx.lock_fd, fcntl.LOCK_UN)
        os.close(ctx.lock_fd)
        ctx.lock_fd = None


@contextlib.contextmanager
def ignoring(*signals: int) -> Iterator[None]:
    """Keep going through these signals (commands started meanwhile inherit it)."""
    before = [(s, signal.signal(s, signal.SIG_IGN)) for s in signals]
    try:
        yield
    finally:
        for s, handler in before:
            signal.signal(s, handler)


def confirm(ctx: Ctx, question: str, default_yes: bool = False) -> None:
    """Ask before a change; --yes and --dry-run skip it. No means stop."""
    if ctx.runner.dry_run or ctx.args.yes:
        return
    if not ui.have_tty():
        raise WgtError(f'no terminal to confirm "{question}"; re-run with --yes')
    if not ui.yes(question, default_yes):
        raise WgtError(f"aborted at: {ctx.runner.current_step} (nothing further changed; --yes skips prompts)")


def will_ask(ctx: Ctx) -> bool:
    """confirm would put its question to someone (not --yes, --dry-run, or no terminal)."""
    return not (ctx.runner.dry_run or ctx.args.yes) and ui.have_tty()


def lock_unless_asking(ctx: Ctx) -> None:
    """Where a command's decisions start: with no one to ask, the lock comes first, as
    for any change. With someone to ask, it waits for confirm_then_lock: a watchdog skips
    its runs while a command holds it, and an answer may take an hour."""
    if not will_ask(ctx):
        take_lock(ctx)


def lock_as_seen(ctx: Ctx) -> None:
    """Take the lock for decisions already made on ctx.snap (read without it, after
    lock_unless_asking). Another command that changed the server's files meanwhile
    stops this one: what it decided, or asked about, may no longer hold."""
    seen = ctx.snap
    try:
        take_lock(ctx)
    except WgtError as e:   # after a yes, too: say it did nothing
        raise WgtError(f"{e}; nothing was done" if "run it again" in str(e) else f"{e}: nothing was done, "
                       "run it again") from None
    if ctx.snap is not seen and ctx.snap != seen:
        raise WgtError("another wireguard-tools command changed this server meanwhile; "
                       "nothing was done: run it again")


def confirm_then_lock(ctx: Ctx, question: str, default_yes: bool = False) -> None:
    """confirm, then lock_as_seen."""
    confirm(ctx, question, default_yes)
    lock_as_seen(ctx)


def is_set_up(ctx: Ctx) -> bool:
    """`wgt setup` finished here, and hasn't been rolled back."""
    return any(store.state_get(ctx.snap.state, key) for key in SET_UP_KEYS)


def require_setup(ctx: Ctx) -> None:
    """For commands that need only `wgt setup` to have run (jumps)."""
    if not is_set_up(ctx):
        raise WgtError("not set up; run 'wgt setup'")


def require_server(ctx: Ctx) -> None:
    """For commands that write client configs: setup ran and the server's keys
    and wg0.conf exist. A dry run only warns, to show what would happen."""
    if is_set_up(ctx) and os.path.isfile(ctx.cfg.wg_conf) and store.is_nonempty(ctx.paths.server_pub):
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


def state_unset(ctx: Ctx, *keys: str) -> None:
    """Drop <keys> from wg-tools.state, in one write (dry-run aware)."""
    text = store.read_text(ctx.paths.state)
    if text is None:
        return
    if ctx.runner.dry_run:
        ctx.out.info(f"[dry-run] would clear {' '.join(keys)} in {ctx.paths.state}")
        return
    for key in keys:
        text = store.state_unset(text, key)
    write_private(ctx.paths.state, text)
    ctx.reload()


# --- endpoint ---

def resolve_endpoint(ctx: Ctx) -> str:
    """Client endpoint: --endpoint / ENDPOINT, the saved value, or the detected
    public IPv4 (detected once, by setup's review, which shows it before it's
    confirmed). Left in ctx.cfg, not saved: setup saves it with the other
    settings (server.save_config), once nothing more can be answered no."""
    src = ctx.cfg.sources.get("ENDPOINT", "")
    ep = ctx.cfg.values.get("ENDPOINT", "") if src in ("flag", "env", "detected") else ""
    ep = ep or store.state_get(ctx.snap.state, "ENDPOINT")
    if not ep:
        ep = live.trace_field(live.trace(ctx.runner, "-4"), "ip")
        if not ep:
            raise WgtError("could not detect the public IP; wgt setup --endpoint <ip-or-host>")
    ep = config.bracketed(ep)
    ctx.cfg.values["ENDPOINT"] = ep
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


def add_live_peer(ctx: Ctx, name: str, pub: str, ip: str) -> None:
    ctx.runner.run(["wg", "set", ctx.cfg.WG_IF, "peer", pub, "preshared-key",
                    ctx.paths.psk(name), "allowed-ips", ctx.cfg.client_addrs(ip)])
    ctx.out.info(f"peer live on {ctx.cfg.WG_IF} (other clients unaffected)")


def remove_live_peer(ctx: Ctx, pub: str) -> None:
    ctx.runner.run(["wg", "set", ctx.cfg.WG_IF, "peer", pub, "remove"])


def wg_up(ctx: Ctx) -> bool:
    return live.if_exists(ctx.runner, ctx.cfg.WG_IF)


# --- the databases ---

# The database writes below read through ctx.files, so a dry run's second
# change to the same file builds on its first (rename = add new + drop old),
# and each reloads ctx.snap: snapshot values read before them are stale after.

def _rewrite(ctx: Ctx, db: str, change: Callable[[Optional[str]], str]) -> None:
    """<db> (mode 600) as <change> makes it from its text now, in one write; reloads ctx.snap."""
    ctx.files.write(db, 0o600, change(ctx.files.read(db)))
    ctx.reload()


def save_client(ctx: Ctx, entry: store.Client) -> None:
    """Add <entry> to clients.db, replacing any client of that name; reloads ctx.snap."""
    _rewrite(ctx, ctx.paths.client_db, lambda text: store.put_client(text, entry.name, entry))


def remove_client(ctx: Ctx, name: str) -> None:
    """Drop <name> from clients.db; reloads ctx.snap."""
    _rewrite(ctx, ctx.paths.client_db, lambda text: store.put_client(text, name, None))


def rename_client(ctx: Ctx, old: str, entry: store.Client) -> None:
    """<old>'s entry replaced by <entry> in one write: stopping in between must
    not lose its public key. Reloads ctx.snap."""
    _rewrite(ctx, ctx.paths.client_db,
             lambda text: store.put_client(store.put_client(text, old, None), entry.name, entry))


def replace_clients(ctx: Ctx, entries: List[store.Client]) -> None:
    """clients.db as exactly <entries>, in one write (setup's subnet move: a stop part-way
    must not leave some configs moved); reloads ctx.snap."""
    _rewrite(ctx, ctx.paths.client_db, lambda _: store.format_clients(e.fields_line() for e in entries))


def set_disabled(ctx: Ctx, name: str, disabled: bool) -> None:
    """Mark <name> disabled (or not) in clients.disabled; reloads ctx.snap."""
    set_disabled_many(ctx, [name], disabled)


def set_disabled_many(ctx: Ctx, names: List[str], disabled: bool) -> None:
    """Mark each of <names> disabled (or not) in clients.disabled, in one write; reloads ctx.snap."""
    def change(text: Optional[str]) -> str:
        for name in names:
            text = store.set_disabled(text, name, disabled)
        return text or ""
    _rewrite(ctx, ctx.paths.disabled_db, change)


def save_exit(ctx: Ctx, entry: store.Exit) -> None:
    """Add <entry> to exits.db, replacing any exit of that code; reloads ctx.snap."""
    _rewrite(ctx, ctx.paths.exit_db, lambda text: store.put_exit(text, entry.code, entry))


def remove_exit(ctx: Ctx, code: str) -> None:
    """Drop <code> from exits.db; reloads ctx.snap."""
    _rewrite(ctx, ctx.paths.exit_db, lambda text: store.put_exit(text, code, None))


def set_device(ctx: Ctx, name: str, number: Optional[int], groups: Optional[Tuple[str, ...]] = None) -> None:
    """Give device <name> a static <number>, or none, and <groups> (None: the
    ones it has); reloads ctx.snap."""
    _rewrite(ctx, ctx.paths.device_db, lambda text: store.put_device(text, name, number, groups))


def rename_device(ctx: Ctx, old: str, new: str) -> None:
    """Move <old>'s number and groups to <new>, in one write; reloads ctx.snap."""
    _rewrite(ctx, ctx.paths.device_db, lambda text: store.rename_device(text, old, new))


# --- command words ---

Command = Callable[..., Optional[int]]   # (ctx, words) -> exit status or None


def dispatch(ctx: Ctx, noun: str, words: List[str], table: Mapping[str, Command]) -> Optional[int]:
    """wgt <noun> <verb> ...: run the verb's command (no verb: list). An
    unknown verb's error lists the verbs from <table>."""
    verb, rest = (words[0], words[1:]) if words else ("list", [])
    fn = table.get(verb)
    if fn is None:
        raise WgtError(f"unknown {noun} command: {verb} ({', '.join(table)}; see: wgt help)")
    return fn(ctx, rest)


def no_more_words(extra: List[str], usage: str) -> None:
    """Refuse leftover words (`wgt client remove a b` must not remove only a)."""
    if extra:
        raise WgtError(f"unexpected: {' '.join(extra)} (usage: {usage})")
