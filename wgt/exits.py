"""Exit cities: clients named <code>.<name> leave through a Proton VPN city
with WARP inside it (px-<code>, with wx-<code> inside). A city is only up
while one of its clients is connected (`wgt watchdog exits` brings it up and
down); while it is down its clients are blocked, never sent out another way."""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from typing import TYPE_CHECKING, List, Optional, Tuple

from . import live, ops, render, results, rules, store, units, warp
from .const import EXIT_IDLE, EXIT_MAX, EXIT_WATCHDOG, WATCHDOG_STALE
from .errors import WgtError

if TYPE_CHECKING:
    from .cli import Ctx


def is_up(ctx: Ctx, code: str) -> bool:
    """Both tunnels of an exit are up (any_up: either one)."""
    return live.if_exists(ctx.runner, f"px-{code}") and live.if_exists(ctx.runner, f"wx-{code}")


def any_up(ctx: Ctx, code: str) -> bool:
    return live.if_exists(ctx.runner, f"px-{code}") or live.if_exists(ctx.runner, f"wx-{code}")


def active_count(ctx: Ctx, code: str) -> int:
    """Clients of <code> with a handshake within EXIT_IDLE (what keeps it up)."""
    hs = live.handshakes(ctx.runner, ctx.cfg.WG_IF)
    now = time.time()
    n = 0
    for c in ctx.snap.exit_clients(code):
        if ctx.snap.is_disabled(c.name):
            continue
        ts = hs.get(c.pub, 0)
        if ts > 0 and now - ts <= EXIT_IDLE:
            n += 1
    return n


def print_list(ctx: Ctx) -> None:
    """The exit cities table: code, name, up (and active clients) or idle,
    clients, Proton server."""
    out = ctx.out
    if not ctx.snap.exits:
        out.info("no exit cities (add one: wgt exit add <code> <proton.conf>)")
        return
    fmt = "{:<7} {:<22} {:<18} {:<8} {}"
    out.info(fmt.format("CODE", "NAME", "STATE", "CLIENTS", "PROTON"))
    for e in ctx.snap.exits:
        n = len(ctx.snap.exit_clients(e.code))
        state = f"up ({active_count(ctx, e.code)} active)" if is_up(ctx, e.code) else "idle"
        px = store.read_text(ctx.paths.px_conf(e.code)) or ""
        ep = next((line[len("Endpoint = "):] for line in px.splitlines() if line.startswith("Endpoint = ")), "")
        out.info(fmt.format(e.code, e.label, state, n, ep or "?"))


# --- configs, up and down ---

def check_proton_conf(text: str) -> None:
    """A Proton WireGuard config must have one interface key and IPv4 address
    and one peer with a key and endpoint."""
    for k in ("PrivateKey", "PublicKey", "Endpoint"):
        if sum(1 for line in text.splitlines() if re.match(rf"^\s*{k}\s*=", line)) != 1:
            raise WgtError(f"not a single-peer WireGuard config (need one {k})")
    if not re.search(r"^\s*Address\s*=.*[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+", text, re.M):
        raise WgtError("the config has no IPv4 Address")


def write_confs(ctx: Ctx, e: store.Exit) -> bool:
    """Write an exit's two tunnel configs; True if either changed."""
    from .ops import ensure_managed
    px, wx = ctx.paths.px_conf(e.code), ctx.paths.wx_conf(e.code)
    for f in (px, wx):
        ensure_managed(ctx, f)
    raw = store.read_text(ctx.paths.proton_conf(e.code)) or ""
    changed = ctx.files.write_config(px, 0o600, render.render_proton_conf(raw, e))
    profile = ctx.paths.wgcf_profile(ctx.paths.exit_wgcf_dir(e.code))
    if os.path.isfile(profile):
        changed |= ctx.files.write_config(wx, 0o600,
                                          warp.render_profile(ctx, profile, rules.exit_wtable(e), rules.exit_mark(e)))
    return changed


def hold(ctx: Ctx, code: str) -> None:
    """Hold off the exit watchdog from <code> for a minute (while we check it)."""
    if not ctx.runner.dry_run:
        with open(ctx.paths.exit_hold(code), "w"):
            pass


def unhold(ctx: Ctx, code: str) -> None:
    ctx.files.remove(ctx.paths.exit_hold(code))


def up(ctx: Ctx, code: str) -> None:
    hold(ctx, code)
    ctx.runner.run(["systemctl", "start", f"wg-quick@px-{code}", f"wg-quick@wx-{code}"])


def down(ctx: Ctx, code: str) -> None:
    """Take an exit down completely (and forget the watchdog's markers)."""
    ctx.runner.run(["systemctl", "stop", f"wg-quick@wx-{code}", f"wg-quick@px-{code}"], check=False)
    for ifc in (f"wx-{code}", f"px-{code}"):
        if live.if_exists(ctx.runner, ifc):
            ctx.runner.run(["ip", "link", "del", "dev", ifc])
    marker = ctx.paths.exit_marker(code)
    ctx.files.remove(marker, marker + ".port")
    unhold(ctx, code)


def wait_hs(ctx: Ctx, code: str, seconds: int = 10) -> None:
    """Give a just-started exit up to <seconds> for its WARP handshake."""
    for _ in range(seconds):
        if live.newest_hs(ctx.runner, f"wx-{code}") > 0:
            return
        time.sleep(1)


def trace(ctx: Ctx, code: str) -> str:
    """"ip=... colo=... loc=... warp=..." through an exit's WARP ("" if no answer)."""
    return live.trace_summary(live.trace(ctx.runner, "--interface", f"wx-{code}"))


def check(ctx: Ctx, code: str) -> Tuple[bool, str]:
    """Bring an exit up if needed and see whether traffic passes through its
    WARP. It is left up only if it was up."""
    was_up = is_up(ctx, code)
    hold(ctx, code)
    try:
        if not was_up:
            up(ctx, code)
        wait_hs(ctx, code)
        t = trace(ctx, code)
    except WgtError as e:
        t = f"could not bring it up: {e}"
    finally:
        if was_up:
            unhold(ctx, code)
        else:
            down(ctx, code)
    return live.is_warp_trace(t), t or "no answer"


def sync(ctx: Ctx) -> None:
    """Setup re-run: re-render every exit's configs (MTU, IPv6 may have changed),
    restart the ones that are up and changed, and keep the watchdog on."""
    if not ctx.snap.exits:
        return
    ctx.out.log("Exit cities: " + " ".join(e.code for e in ctx.snap.exits))
    for e in ctx.snap.exits:
        if write_confs(ctx, e) and any_up(ctx, e.code):
            ctx.runner.run(["systemctl", "restart", f"wg-quick@px-{e.code}", f"wg-quick@wx-{e.code}"])
    units.write_exit_watchdog(ctx)


def free_idx(ctx: Ctx) -> int:
    """The lowest exit index not in use (it picks the city's tables and fwmark)."""
    used = {e.idx for e in ctx.snap.exits}
    for i in range(1, EXIT_MAX + 1):
        if i not in used:
            return i
    raise WgtError(f"at most {EXIT_MAX} exit cities")


# --- commands ---

def cmd_list(ctx: Ctx, words: List[str]) -> None:
    print_list(ctx)


def cmd_add(ctx: Ctx, words: List[str]) -> None:
    """exit add <code> <proton.conf|->: import (or replace) a Proton WireGuard
    config as exit city <code>, give it its own WARP registration, wire its
    routing into wg0 live, and check it end to end."""
    if len(words) < 2:
        raise WgtError('usage: wgt exit add <code> <proton.conf|-> [--label "Paris, France"]')
    code, src = words[0], words[1]
    if not store.valid_exit_code(code):
        raise WgtError("exit code must be 2-6 of a-z0-9 and -, starting with a letter, not 'warp' "
                       f"(e.g. par-fr): '{code}'")
    ops.take_lock(ctx)
    if src == "-":
        raw = sys.stdin.read()
    else:
        raw = store.read_text(src)
        if raw is None:
            raise WgtError(f"no such file: {src}")
    check_proton_conf(raw)
    existing = ctx.snap.exit(code)
    label = (ctx.args.exit_label or (existing.label if existing else "") or render.proton_hint(raw) or code)
    label = label.replace("\n", "").replace("\t", "")
    if existing is None:
        idx = free_idx(ctx)
        ep = next((re.sub(r"^\s*Endpoint\s*=\s*", "", line) for line in raw.splitlines()
                   if re.match(r"^\s*Endpoint\s*=", line)), "")
        ctx.runner.step(f"Add exit city '{code}' ({label}): Proton {ep} -> its own WARP", f"wgt exit remove {code}")
    else:
        idx = existing.idx
        ctx.runner.step(f"Update exit city '{code}' ({label})",
                        f"wgt exit add {code} <the previous copy in "
                        f"{ctx.files.change_dir(ctx.paths.proton_conf(code))}>")
    ops.confirm(ctx, f"Set up exit {code}?", default_yes=True)
    if not warp.install_wgcf(ctx):
        raise WgtError("wgcf is needed for the exit's WARP")
    if not ctx.runner.dry_run:
        os.makedirs(ctx.paths.exit_dir, mode=0o700, exist_ok=True)
    if not warp.register(ctx, ctx.paths.exit_wgcf_dir(code)):
        raise WgtError(f"could not register a WARP account for exit '{code}'")
    body = "".join(line + "\n" for line in raw.splitlines() if not line.startswith(f"# exit {code}: "))
    ctx.files.write(ctx.paths.proton_conf(code), 0o600, f"# exit {code}: {label}\n" + body)
    e = store.Exit(code, idx, label)
    ctx.files.write(ctx.paths.exit_db, 0o600, store.put_exit(store.read_text(ctx.paths.exit_db), code, e))
    ctx.reload()
    if ctx.runner.dry_run:
        ctx.out.info(f"[dry-run] would write {ctx.paths.wg(f'px-{code}.conf')} and wx-{code}.conf, "
                     f"add its routing to {ctx.cfg.wg_conf} and check it")
        return
    changed = write_confs(ctx, e)
    ops.write_wg_conf(ctx)
    if ops.wg_up(ctx):
        ctx.runner.sh_lines(rules.exit_hook_cmds(ctx.cfg, ctx.snap, e, "up"))
        ctx.runner.sh_lines(rules.ipt_cmds(rules.ipt_rules_out(ctx.cfg, f"wx-{code}"), "up"))
    units.open_firewall(ctx, rules.out_ifs(ctx.cfg, ctx.pub_if, ctx.snap.exits))
    units.write_exit_watchdog(ctx)
    # A changed config (or a half-up exit) must restart to load it.
    if changed and any_up(ctx, code):
        down(ctx, code)
    ctx.out.log(f"Checking exit '{code}'")
    ok, t = check(ctx, code)
    if ok:
        ctx.out.info(f"exit '{code}' works: {t}")
    else:
        ctx.out.warn(f"exit '{code}' isn't passing traffic yet ({t}); check: wgt exit test")
    ctx.out.info(f"it goes down when idle and comes up when a client connects; add one: "
                 f"wgt client add <name> --exit {code}")


def cmd_remove(ctx: Ctx, words: List[str]) -> None:
    """exit remove <code>: refused while clients use it; removes its tunnels,
    routing, firewall rules, WARP account and Proton config (and their copies)."""
    code = words[0] if words else ""
    ops.take_lock(ctx)
    e = ctx.snap.exit(code) if code else None
    if e is None:
        raise WgtError(f"no exit city '{code}' (see: wgt exit list)")
    users = " ".join(c.name for c in ctx.snap.exit_clients(code))
    if users:
        raise WgtError(f"clients use exit '{code}': {users}; remove them first (wgt client remove <name>)")
    ctx.runner.step(f"Remove exit city '{code}' ({e.label}): its tunnels, routing, WARP account and Proton config",
                    f"wgt exit add {code} <proton.conf>")
    ops.confirm(ctx, f"Remove exit {code}?")
    down(ctx, code)
    if ops.wg_up(ctx):
        ctx.runner.sh_lines(rules.ipt_cmds(list(reversed(rules.ipt_rules_out(ctx.cfg, f"wx-{code}"))), "down"))
        ctx.runner.sh_lines(rules.exit_hook_cmds(ctx.cfg, ctx.snap, e, "down"))
    if store.state_get(ctx.snap.state, "UFW") == "1" and shutil.which("ufw"):
        ctx.runner.run(["ufw", "route", "delete", "allow", "in", "on", ctx.cfg.WG_IF, "out", "on", f"wx-{code}"],
                       check=False)
    ctx.files.write(ctx.paths.exit_db, 0o600, store.put_exit(store.read_text(ctx.paths.exit_db), code))
    ctx.reload()
    ops.write_wg_conf(ctx)
    for f in (ctx.paths.px_conf(code), ctx.paths.wx_conf(code),
              ctx.paths.proton_conf(code)):
        ctx.files.forget(f)
    ctx.files.remove_tree(ctx.paths.exit_wgcf_dir(code))
    if not ctx.snap.exits:
        units.remove_timer(ctx, EXIT_WATCHDOG)
        ctx.runner.run(["systemctl", "daemon-reload"])


def cmd_test(ctx: Ctx, words: List[str]) -> int:
    """wgt exit test: bring each city up and check it end to end."""
    ops.take_lock(ctx)
    ctx.out.log("Exit city tests")
    res = results.Results()
    run_tests(ctx, res, bring_up=True)
    return results.report(ctx, res)


def cmd_exit(ctx: Ctx, words: List[str]) -> Optional[int]:
    """wgt exit <command>: dispatch; no command means list ("rm" is kept as
    an alias of remove)."""
    ops.require_setup(ctx)
    sub, rest = (words[0], words[1:]) if words else ("list", [])
    table = {"list": cmd_list, "add": cmd_add, "remove": cmd_remove, "rm": cmd_remove, "test": cmd_test}
    fn = table.get(sub)
    if fn is None:
        raise WgtError(f"unknown exit command: {sub} (list, add, remove, test; see: wgt help)")
    return fn(ctx, rest)


# --- checks ---

def tables_ok(ctx: Ctx, e: store.Exit) -> bool:
    """Its tables have blackhole fallbacks and the fwmark rule points at Proton."""
    r = ctx.runner
    w, p, m = rules.exit_wtable(e), rules.exit_ptable(e), rules.exit_mark(e)
    if f"fwmark {m:#x} lookup {p}" not in live.rules(r):
        return False
    if "\nblackhole default" not in "\n" + live.table(r, "ip", p):
        return False
    return all("\nblackhole default" in "\n" + live.table(r, f, w) for f in ctx.cfg.families())


def client_rules_ok(ctx: Ctx, e: store.Exit) -> bool:
    """One rule per client address."""
    w = rules.exit_wtable(e)
    n = len(ctx.snap.exit_clients(e.code))
    for f in ctx.cfg.families():
        found = sum(1 for line in live.rules(ctx.runner, f).splitlines() if re.search(rf"lookup {w}\b", line))
        if found != n:
            return False
    return True


def run_tests(ctx: Ctx, res: results.Results, bring_up: bool = False) -> None:
    """The exit-city checks; with <bring_up> (exit test) an idle exit is brought
    up for its tunnel checks and taken down again."""
    r = ctx.runner
    if not ctx.snap.exits:
        res.result("SKIP", "exit cities (none)")
        return
    for e in ctx.snap.exits:
        res.check(f"exit {e.code}: tables with blackhole fallbacks, fwmark rule to Proton", tables_ok(ctx, e))
        res.check(f"exit {e.code}: one rule per address of its {len(ctx.snap.exit_clients(e.code))} client(s)",
                  client_rules_ok(ctx, e))
        act = active_count(ctx, e.code)
        started = False
        if bring_up and not is_up(ctx, e.code):
            started = True
            try:
                up(ctx, e.code)
            except WgtError as err:
                res.result("FAIL", f"exit {e.code}: could not bring it up ({err})")
                down(ctx, e.code)
                continue
        if is_up(ctx, e.code):
            hold(ctx, e.code)
            wait_hs(ctx, e.code)
            res.check(f"exit {e.code}: Proton tunnel px-{e.code} handshake within {WATCHDOG_STALE}s",
                      live.hs_recent(r, f"px-{e.code}", WATCHDOG_STALE))
            res.check(f"exit {e.code}: WARP tunnel wx-{e.code} handshake within {WATCHDOG_STALE}s",
                      live.hs_recent(r, f"wx-{e.code}", WATCHDOG_STALE))
            tr = live.trace_summary(live.trace(r, "--interface", f"wx-{e.code}"))
            if live.is_warp_trace(tr):
                res.result("PASS", f"exit {e.code}: traffic exits through WARP ({tr})")
            else:
                res.result("FAIL", f"exit {e.code}: no WARP trace through wx-{e.code} ({tr or 'no answer'})")
            if started:
                down(ctx, e.code)
            else:
                unhold(ctx, e.code)
        elif act > 0:
            res.result("FAIL", f"exit {e.code}: down while {act} client(s) are active "
                               "(the watchdog brings it up within 10s)")
        else:
            res.result("PASS", f"exit {e.code}: idle, no active clients (comes up when one connects; "
                               f"'wgt exit test' checks it live)")
    res.check("exit watchdog timer enabled and running",
              live.unit_enabled(r, f"{EXIT_WATCHDOG}.timer") and live.unit_active(r, f"{EXIT_WATCHDOG}.timer"))
