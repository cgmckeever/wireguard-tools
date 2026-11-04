"""Jumps: clients named <code>.<name> leave through another WireGuard server
(a VPN provider's, e.g. Proton VPN, or your own), with its own WARP inside it
unless the client has WARP off (px-<code>, with wx-<code> inside). A jump is only up
while one of its clients is connected (`wgt watchdog exits` brings it up and
down); while it is down its clients are blocked, never sent out another way."""

from __future__ import annotations

import contextlib
import os
import re
import sys
import time
from typing import TYPE_CHECKING, Dict, Iterator, List, Optional, Tuple

from . import live, ops, render, results, rules, store, system, ui, units, warp
from .const import EXIT_IDLE, EXIT_MAX, EXIT_WATCHDOG, WATCHDOG_STALE
from .errors import WgtError

if TYPE_CHECKING:
    from .ctx import Ctx


def is_up(ctx: Ctx, e: store.Exit) -> bool:
    """Both of an exit's tunnels are up."""
    return live.if_exists(ctx.runner, e.px) and live.if_exists(ctx.runner, e.wx)


def any_up(ctx: Ctx, e: store.Exit) -> bool:
    """Either of an exit's tunnels is up (a half-up exit still needs taking down)."""
    return live.if_exists(ctx.runner, e.px) or live.if_exists(ctx.runner, e.wx)


def active_clients(snap: store.Snapshot, code: str, handshakes: Dict[str, int], now: float) -> List[store.Client]:
    """The clients that keep exit <code> up: enabled, with a handshake within
    EXIT_IDLE seconds. The one rule for `exit list`, `exit test` and the watchdog."""
    return [c for c in snap.exit_clients(code)
            if not snap.is_disabled(c.name) and handshakes.get(c.pub, 0) > 0 and now - handshakes[c.pub] <= EXIT_IDLE]


def active_count(ctx: Ctx, code: str) -> int:
    """How many of <code>'s clients are active right now."""
    return len(active_clients(ctx.snap, code, live.handshakes(ctx.runner, ctx.cfg.WG_IF), ctx.clock()))


def print_list(ctx: Ctx) -> None:
    """The jumps table: code, name, up (and active clients) or idle,
    clients, the jump's endpoint."""
    out = ctx.out
    if not ctx.snap.exits:
        out.info("no jumps (add one: wgt jump add <code> <wg.conf>)")
        return
    fmt = "{:<7} {:<22} {:<18} {:<8} {}"
    out.info(fmt.format("CODE", "NAME", "STATE", "CLIENTS", "ENDPOINT"))
    for e in ctx.snap.exits:
        n = len(ctx.snap.exit_clients(e.code))
        state = f"up ({active_count(ctx, e.code)} active)" if is_up(ctx, e) else "idle"
        ep = render.profile_endpoint(store.read_text(ctx.paths.px_conf(e.code)) or "")
        out.info(fmt.format(e.code, e.label, state, n, ep or "?"))


# --- configs, up and down ---

def check_jump_conf(text: str) -> None:
    """A jump's WireGuard config must have one interface key and IPv4 address
    and one peer with a key and endpoint."""
    for k in ("PrivateKey", "PublicKey", "Endpoint"):
        if sum(1 for line in text.splitlines() if re.match(rf"^\s*{k}\s*=", line)) != 1:
            raise WgtError(f"not a single-peer WireGuard config (need one {k})")
    if not re.search(r"^\s*Address\s*=.*[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+", text, re.M):
        raise WgtError("the config has no IPv4 Address")


def write_confs(ctx: Ctx, e: store.Exit) -> bool:
    """Write an exit's two tunnel configs; True if either changed."""
    px, wx = ctx.paths.px_conf(e.code), ctx.paths.wx_conf(e.code)
    for path in (px, wx):
        ops.ensure_managed(ctx, path)
    raw = store.read_text(ctx.paths.jump_source(e.code)) or ""
    changed = ctx.files.write_config(px, 0o600, render.render_jump_conf(raw, e))
    profile = ctx.paths.wgcf_profile(ctx.paths.exit_wgcf_dir(e.code))
    if os.path.isfile(profile):
        changed |= ctx.files.write_config(wx, 0o600,
                                          warp.render_profile(ctx, profile, rules.exit_wtable(e), rules.exit_mark(e)))
    return changed


def hold(ctx: Ctx, code: str) -> None:
    """Hold off the exit watchdog from <code> for a minute (while we check it)."""
    if not ctx.runner.dry_run:
        system.write_marker(ctx.paths.exit_hold(code))


def unhold(ctx: Ctx, code: str) -> None:
    """Let the exit watchdog manage <code> again."""
    ctx.files.remove(ctx.paths.exit_hold(code))


def up(ctx: Ctx, e: store.Exit) -> None:
    """Start an exit's tunnels, held from the watchdog until the caller is done."""
    hold(ctx, e.code)
    ctx.runner.run(["systemctl", "start", *e.units])


def down(ctx: Ctx, e: store.Exit, check: bool = True) -> bool:
    """Take an exit down completely and forget the watchdog's markers; True
    if both tunnels are gone. With check=False nothing raises (the watchdog
    must finish its run whatever happens)."""
    r = ctx.runner
    r.run(["systemctl", "stop", *reversed(e.units)], check=False)
    for ifc in (e.wx, e.px):
        if live.if_exists(r, ifc):
            r.run(["ip", "link", "del", "dev", ifc], check=check)
    for path in (ctx.paths.exit_marker(e.code), ctx.paths.exit_port_marker(e.code), ctx.paths.exit_hold(e.code)):
        with contextlib.suppress(OSError):
            ctx.files.remove(path)
    return not any_up(ctx, e)


def wait_hs(ctx: Ctx, e: store.Exit, seconds: int = 10) -> None:
    """Give a just-started exit up to <seconds> for its WARP handshake."""
    for _ in range(seconds):
        if live.newest_hs(ctx.runner, e.wx) > 0:
            return
        time.sleep(1)


def trace(ctx: Ctx, e: store.Exit) -> str:
    """"ip=... colo=... loc=... warp=..." through an exit's WARP ("" if no answer)."""
    return live.trace_summary(live.trace(ctx.runner, "--interface", e.wx))


@contextlib.contextmanager
def probing(ctx: Ctx, e: store.Exit) -> Iterator[None]:
    """Have an exit up (and held from the watchdog) for a check, then leave it
    as it was: taken down again if this started it. Raises WgtError if it
    can't be brought up (and takes it down again)."""
    was_up = is_up(ctx, e)
    hold(ctx, e.code)
    try:
        if not was_up:
            up(ctx, e)
        wait_hs(ctx, e)
        yield
    finally:
        if was_up:
            unhold(ctx, e.code)
        else:
            down(ctx, e)


def probe(ctx: Ctx, e: store.Exit) -> Tuple[bool, str]:
    """Does traffic pass through the exit's WARP? (ok, what the trace said)."""
    try:
        with probing(ctx, e):
            t = trace(ctx, e)
    except WgtError as err:
        t = f"could not bring it up: {err}"
    return live.is_warp_trace(t), t or "no answer"


def sync(ctx: Ctx) -> None:
    """Setup re-run: re-render every exit's configs (MTU, IPv6 may have changed),
    restart the ones that are up and changed, and keep the watchdog on."""
    if not ctx.snap.exits:
        return
    ctx.out.log("Jumps: " + " ".join(e.code for e in ctx.snap.exits))
    for e in ctx.snap.exits:
        if write_confs(ctx, e) and any_up(ctx, e):
            ctx.runner.run(["systemctl", "restart", *e.units])
    units.write_exit_watchdog(ctx)


def free_idx(ctx: Ctx) -> int:
    """The lowest exit index not in use (it picks the jump's tables and fwmark)."""
    used = {e.idx for e in ctx.snap.exits}
    for i in range(1, EXIT_MAX + 1):
        if i not in used:
            return i
    raise WgtError(f"at most {EXIT_MAX} jumps")


# --- commands ---

def cmd_list(ctx: Ctx, words: List[str]) -> None:
    """wgt jump list"""
    ops.no_more_words(words, "wgt jump list")
    print_list(ctx)


def pick_exit(ctx: Ctx, verb: str) -> str:
    """No code given: a numbered list of jumps to pick from (needs a terminal)."""
    if not ctx.snap.exits:
        raise WgtError("no jumps (add one: wgt jump add <code> <wg.conf>)")
    if not ui.have_tty() or ctx.args.yes:
        raise WgtError(f"usage: wgt jump {verb} <code>")
    try:
        i = ui.menu("", [f"{e.code:<7} {e.label}" for e in ctx.snap.exits], f"{verb.capitalize()} which jump?")
    except ValueError as err:
        if ctx.snap.exit(str(err)):
            return str(err)
        raise WgtError(f"no jump '{err}'") from None
    if i is None:
        raise WgtError("no jump chosen")
    return ctx.snap.exits[i].code


def cmd_add(ctx: Ctx, words: List[str]) -> None:
    """exit add <code> <wg.conf|->: import (or replace) a WireGuard
    config as jump <code>, give it its own WARP registration, wire its
    routing into wg0 live, and check it end to end."""
    usage = 'wgt jump add <code> <wg.conf|-> [--label "Paris, France"]'
    if len(words) < 2:
        raise WgtError(f"usage: {usage}")
    ops.no_more_words(words[2:], usage)
    code, src = words[0], words[1]
    if not store.valid_exit_code(code):
        raise WgtError(f"jump code must be {store.EXIT_CODE_RULE}: '{code}'")
    ops.take_lock(ctx)
    raw = _read_jump(src)
    existing = ctx.snap.exit(code)
    label = (ctx.args.exit_label or (existing.label if existing else "") or render.peer_hint(raw) or code)
    label = label.replace("\n", "").replace("\t", "")
    if existing is None:
        idx = free_idx(ctx)
        ctx.runner.step(f"Add jump '{code}' ({label}): {render.profile_endpoint(raw)} -> its own WARP",
                        f"wgt jump remove {code}")
    else:
        idx = existing.idx
        ctx.runner.step(f"Update jump '{code}' ({label})",
                        f"wgt jump add {code} <the previous copy in "
                        f"{ctx.files.change_dir(ctx.paths.jump_source(code))}>")
    ops.confirm(ctx, f"Set up jump {code}?", default_yes=True)
    if not warp.install_wgcf(ctx):
        raise WgtError("wgcf is needed for the jump's WARP")
    if not ctx.runner.dry_run:
        os.makedirs(ctx.paths.exit_dir, mode=0o700, exist_ok=True)
    if not warp.register(ctx, ctx.paths.exit_wgcf_dir(code)):
        raise WgtError(f"could not register a WARP account for jump '{code}'")
    # The stored copy carries its label as the first line (exit list, rollback).
    body = "".join(line + "\n" for line in raw.splitlines() if not line.startswith(f"# exit {code}: "))
    ctx.files.write(ctx.paths.jump_source(code), 0o600, f"# exit {code}: {label}\n" + body)
    e = store.Exit(code, idx, label)
    ops.save_exit(ctx, e)
    if ctx.runner.dry_run:
        # Its WARP profile doesn't exist yet in a dry run, so wx-<code>.conf
        # can't be rendered; wg0.conf (its routing) still shows as a diff.
        ctx.out.info(f"[dry-run] would write {ctx.paths.px_conf(code)} and {ctx.paths.wx_conf(code)}")
        ops.write_wg_conf(ctx)
        units.open_firewall(ctx, rules.out_ifs(ctx.cfg, ctx.pub_if, ctx.snap.exits))
        ctx.out.info(f"[dry-run] would start the jump watchdog and check jump '{code}'")
        return
    changed = write_confs(ctx, e)
    _wire_live(ctx, e)
    # A changed config (or a half-up exit) must restart to load it.
    if changed and any_up(ctx, e):
        down(ctx, e)
    _check_new(ctx, e)


def _read_jump(src: str) -> str:
    """A jump's WireGuard config from a file or "-" (stdin), checked."""
    if src == "-":
        raw = sys.stdin.read()
    else:
        text = store.read_text(src)
        if text is None:
            raise WgtError(f"no such file: {src}")
        raw = text
    check_jump_conf(raw)
    return raw


def _wire_live(ctx: Ctx, e: store.Exit) -> None:
    """A new exit's routing: into wg0.conf, live on a running wg0, through
    ufw, and the exit watchdog on."""
    ops.write_wg_conf(ctx)
    if ops.wg_up(ctx):
        ctx.runner.sh_lines(rules.exit_hook_cmds(ctx.cfg, ctx.snap, e, up=True))
        ctx.runner.sh_lines(rules.ipt_cmds(rules.exit_ipt_rules(ctx.cfg, e), up=True))
    units.open_firewall(ctx, rules.out_ifs(ctx.cfg, ctx.pub_if, ctx.snap.exits))
    units.write_exit_watchdog(ctx)


def _check_new(ctx: Ctx, e: store.Exit) -> None:
    """Bring the new exit up once and see traffic leave through its WARP."""
    ctx.out.log(f"Checking jump '{e.code}'")
    ok, said = probe(ctx, e)
    if ok:
        ctx.out.info(f"jump '{e.code}' works: {said}")
    else:
        ctx.out.warn(f"jump '{e.code}' isn't passing traffic yet ({said}); check: wgt jump test")
    ctx.out.info(f"it goes down when idle and comes up when a client connects; add one: "
                 f"wgt client add <name> --exit {e.code}")


def cmd_remove(ctx: Ctx, words: List[str]) -> None:
    """exit remove <code>: refused while clients use it; removes its tunnels,
    routing, firewall rules, WARP account and jump config (and their copies)."""
    ops.no_more_words(words[1:], "wgt jump remove <code>")
    code = words[0] if words else pick_exit(ctx, "remove")
    ops.take_lock(ctx)
    e = ctx.snap.exit(code)
    if e is None:
        raise WgtError(f"no jump '{code}' (see: wgt jump list)")
    users = " ".join(c.name for c in ctx.snap.exit_clients(code))
    if users:
        raise WgtError(f"clients use jump '{code}': {users}; remove them first (wgt client remove <name>)")
    ctx.runner.step(f"Remove jump '{code}' ({e.label}): its tunnels, routing, WARP account and jump config",
                    f"wgt jump add {code} <wg.conf>")
    ops.confirm(ctx, f"Remove jump {code}?")
    down(ctx, e)
    if ops.wg_up(ctx):
        ctx.runner.sh_lines(rules.ipt_cmds(reversed(rules.exit_ipt_rules(ctx.cfg, e)), up=False))
        ctx.runner.sh_lines(rules.exit_hook_cmds(ctx.cfg, ctx.snap, e, up=False))
    units.close_firewall(ctx, rules.exit_ifs(e))
    ops.remove_exit(ctx, code)
    ops.write_wg_conf(ctx)
    for path in (ctx.paths.px_conf(code), ctx.paths.wx_conf(code), ctx.paths.jump_source(code)):
        ctx.files.forget(path)
    ctx.files.remove_tree(ctx.paths.exit_wgcf_dir(code))
    if not ctx.snap.exits:
        units.remove_timer(ctx, EXIT_WATCHDOG)
        ctx.runner.run(["systemctl", "daemon-reload"])


def cmd_test(ctx: Ctx, words: List[str]) -> int:
    """wgt jump test: bring each jump up and check it end to end."""
    ops.no_more_words(words, "wgt jump test")
    ops.take_lock(ctx)
    ctx.out.log("Jump tests")
    res = results.Results(out=ctx.out.line)
    run_tests(ctx, res, bring_up=True)
    return results.report(ctx, res)


def cmd_exit(ctx: Ctx, words: List[str]) -> Optional[int]:
    """wgt jump (or exit, the old name) <command>: dispatch; no command means list."""
    ops.require_setup(ctx)
    return ops.dispatch(ctx, "jump", words, EXIT_COMMANDS)


# --- checks ---

def tables_ok(ctx: Ctx, e: store.Exit) -> bool:
    """Its tables have blackhole fallbacks and the fwmark rule points at the jump."""
    r = ctx.runner
    client_table, jump_table, mark = rules.exit_wtable(e), rules.exit_ptable(e), rules.exit_mark(e)
    if f"fwmark {mark:#x} lookup {jump_table}" not in live.rules(r):
        return False
    for fam in ctx.cfg.families():   # IPv6 too: it stops WARP-off clients' IPv6
        if "\nblackhole default" not in "\n" + live.table(r, fam, jump_table):
            return False
    return all("\nblackhole default" in "\n" + live.table(r, fam, client_table) for fam in ctx.cfg.families())


def client_rules_ok(ctx: Ctx, e: store.Exit) -> bool:
    """One rule per client address: to its WARP table, or (WARP off) its jump table."""
    users = ctx.snap.exit_clients(e.code)
    off = sum(1 for c in users if ctx.snap.warp_off(c.name))
    for fam in ctx.cfg.families():
        lines = [line for line in live.rules(ctx.runner, fam).splitlines() if "fwmark" not in line]
        for table, want in ((rules.exit_wtable(e), len(users) - off), (rules.exit_ptable(e), off)):
            if sum(1 for line in lines if re.search(rf"lookup {table}\b", line)) != want:
                return False
    return True


def tunnel_checks(ctx: Ctx, res: results.Results, e: store.Exit) -> None:
    """An up exit: both tunnels handshaking, and traffic leaving through its WARP."""
    for kind, ifc in (("jump", e.px), ("WARP", e.wx)):
        res.check(f"jump {e.code}: {kind} tunnel {ifc} handshake within {WATCHDOG_STALE}s",
                  live.hs_recent(ctx.runner, ifc, WATCHDOG_STALE, ctx.clock()))
    tr = trace(ctx, e)
    if live.is_warp_trace(tr):
        res.result("PASS", f"jump {e.code}: traffic exits through WARP ({tr})")
    else:
        res.result("FAIL", f"jump {e.code}: no WARP trace through {e.wx} ({tr or 'no answer'})")


def run_tests(ctx: Ctx, res: results.Results, bring_up: bool = False) -> None:
    """The jump checks; with <bring_up> (exit test) an idle exit is brought
    up for its tunnel checks and taken down again."""
    r = ctx.runner
    if not ctx.snap.exits:
        res.result("SKIP", "jumps (none)")
        return
    for e in ctx.snap.exits:
        res.check(f"jump {e.code}: tables with blackhole fallbacks, fwmark rule to the jump", tables_ok(ctx, e))
        users = ctx.snap.exit_clients(e.code)
        off = sum(1 for c in users if ctx.snap.warp_off(c.name))
        res.check(f"jump {e.code}: one rule per address of its {len(users)} client(s)"
                  + (f" ({off} with WARP off, via the jump)" if off else ""), client_rules_ok(ctx, e))
        act = active_count(ctx, e.code)
        if bring_up or is_up(ctx, e):
            try:
                with probing(ctx, e):
                    tunnel_checks(ctx, res, e)
            except WgtError as err:
                res.result("FAIL", f"jump {e.code}: could not bring it up ({err})")
        elif act > 0:
            res.result("FAIL", f"jump {e.code}: down while {act} client(s) are active "
                               "(the watchdog brings it up within 10s)")
        else:
            res.result("PASS", f"jump {e.code}: idle, no active clients (comes up when one connects; "
                               f"'wgt jump test' checks it live)")
    res.check("jump watchdog timer enabled and running",
              live.timer_on(r, EXIT_WATCHDOG))


EXIT_COMMANDS = {"list": cmd_list, "add": cmd_add, "remove": cmd_remove, "test": cmd_test}
