"""Exit cities: clients named <code>.<name> leave through a Proton VPN city
with WARP inside it (px-<code>, with wx-<code> inside). A city is only up
while one of its clients is connected (`wgt watchdog exits` brings it up and
down); while it is down its clients are blocked, never sent out another way."""

from __future__ import annotations

import os
import re
import sys
import time
from typing import List, Tuple

from . import live, render, rules, store, units, warp
from .const import EXIT_IDLE, EXIT_MAX, EXIT_WATCHDOG, WATCHDOG_STALE
from .errors import WgtError


def is_up(ctx, code: str) -> bool:
    """Both tunnels of an exit are up (any_up: either one)."""
    return live.if_exists(ctx.runner, f"px-{code}") and live.if_exists(ctx.runner, f"wx-{code}")


def any_up(ctx, code: str) -> bool:
    return live.if_exists(ctx.runner, f"px-{code}") or live.if_exists(ctx.runner, f"wx-{code}")


def active(ctx, code: str) -> int:
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


def print_list(ctx) -> None:
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
        state = f"up ({active(ctx, e.code)} active)" if is_up(ctx, e.code) else "idle"
        px = store.read_text(ctx.paths.wg(f"px-{e.code}.conf")) or ""
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


def write_confs(ctx, e: store.Exit) -> bool:
    """Write an exit's two tunnel configs; True if either changed."""
    from .ops import ensure_managed
    px, wx = ctx.paths.wg(f"px-{e.code}.conf"), ctx.paths.wg(f"wx-{e.code}.conf")
    for f in (px, wx):
        ensure_managed(ctx, f)
    raw = store.read_text(ctx.paths.wg("exits", f"{e.code}.proton.conf")) or ""
    changed = ctx.files.write(px, 0o600, render.render_proton_conf(raw, e))
    profile = ctx.paths.wg("exits", e.code, "wgcf-profile.conf")
    if os.path.isfile(profile):
        changed |= ctx.files.write(wx, 0o600, warp.render_profile(ctx, profile, rules.exit_wtable(e), rules.exit_mark(e)))
    return changed


def hold(ctx, code: str) -> None:
    """Hold off the exit watchdog from <code> for a minute (while we check it)."""
    if not ctx.runner.dry_run:
        with open(os.path.join(ctx.paths.run_dir, f"{EXIT_WATCHDOG}.{code}.hold"), "w"):
            pass


def unhold(ctx, code: str) -> None:
    ctx.files.remove(os.path.join(ctx.paths.run_dir, f"{EXIT_WATCHDOG}.{code}.hold"))


def up(ctx, code: str) -> None:
    hold(ctx, code)
    ctx.runner.run(["systemctl", "start", f"wg-quick@px-{code}", f"wg-quick@wx-{code}"])


def down(ctx, code: str) -> None:
    """Take an exit down completely (and forget the watchdog's markers)."""
    ctx.runner.run(["systemctl", "stop", f"wg-quick@wx-{code}", f"wg-quick@px-{code}"], check=False)
    for ifc in (f"wx-{code}", f"px-{code}"):
        if live.if_exists(ctx.runner, ifc):
            ctx.runner.run(["ip", "link", "del", "dev", ifc])
    marker = os.path.join(ctx.paths.run_dir, f"{EXIT_WATCHDOG}.{code}")
    ctx.files.remove(marker, marker + ".port")
    unhold(ctx, code)


def wait_hs(ctx, code: str, seconds: int = 10) -> None:
    """Give a just-started exit up to <seconds> for its WARP handshake."""
    for _ in range(seconds):
        if live.newest_hs(ctx.runner, f"wx-{code}") > 0:
            return
        time.sleep(1)


def trace(ctx, code: str) -> str:
    """"ip=... colo=... loc=... warp=..." through an exit's WARP ("" if no answer)."""
    return live.trace_summary(live.trace(ctx.runner, "--interface", f"wx-{code}"))


def check(ctx, code: str) -> Tuple[bool, str]:
    """Bring an exit up if needed and see whether traffic passes through its
    WARP. It is left up only if it was up."""
    was_up = is_up(ctx, code)
    if not was_up:
        up(ctx, code)
    hold(ctx, code)
    wait_hs(ctx, code)
    t = trace(ctx, code)
    if was_up:
        unhold(ctx, code)
    else:
        down(ctx, code)
    return live.is_warp_trace(t), t or "no answer"


def sync(ctx) -> None:
    """Setup re-run: re-render every exit's configs (MTU, IPv6 may have changed),
    restart the ones that are up and changed, and keep the watchdog on."""
    if not ctx.snap.exits:
        return
    ctx.out.log("Exit cities: " + " ".join(e.code for e in ctx.snap.exits))
    for e in ctx.snap.exits:
        if write_confs(ctx, e) and any_up(ctx, e.code):
            ctx.runner.run(["systemctl", "restart", f"wg-quick@px-{e.code}", f"wg-quick@wx-{e.code}"])
    units.write_exit_watchdog(ctx)


def free_idx(ctx) -> int:
    used = {e.idx for e in ctx.snap.exits}
    for i in range(1, EXIT_MAX + 1):
        if i not in used:
            return i
    raise WgtError(f"at most {EXIT_MAX} exit cities")


# --- commands ---

def cmd_add(ctx, words: List[str]) -> None:
    """exit add <code> <proton.conf|->: import (or replace) a Proton WireGuard
    config as exit city <code>, give it its own WARP registration, wire its
    routing into wg0 live, and check it end to end."""
    from . import ops
    if len(words) < 2:
        raise WgtError('usage: wgt exit add <code> <proton.conf|-> [--label "Paris, France"]')
    code, src = words[0], words[1]
    if not store.valid_exit_code(code):
        raise WgtError(f"exit code must be 2-6 of a-z0-9 and -, starting with a letter, not 'warp' (e.g. par-fr): '{code}'")
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
        ep = next((re.sub(r"^\s*Endpoint\s*=\s*", "", l) for l in raw.splitlines() if re.match(r"^\s*Endpoint\s*=", l)), "")
        ctx.runner.step(f"Add exit city '{code}' ({label}): Proton {ep} -> its own WARP", f"wgt exit remove {code}")
    else:
        idx = existing.idx
        ctx.runner.step(f"Update exit city '{code}' ({label})", "re-run exit add with the previous config")
    ops.confirm(ctx, f"Set up exit {code}?", default_yes=True)
    if not warp.install_wgcf(ctx):
        raise WgtError("wgcf is needed for the exit's WARP")
    if not ctx.runner.dry_run:
        os.makedirs(ctx.paths.exit_dir, mode=0o700, exist_ok=True)
    if not warp.register(ctx, ctx.paths.wg("exits", code)):
        raise WgtError(f"could not register a WARP account for exit '{code}'")
    body = "".join(l + "\n" for l in raw.splitlines() if not l.startswith(f"# exit {code}: "))
    ctx.files.write(ctx.paths.wg("exits", f"{code}.proton.conf"), 0o600, f"# exit {code}: {label}\n" + body)
    ctx.files.write(ctx.paths.exit_db, 0o600,
                    store.put_exit(store.read_text(ctx.paths.exit_db), code, store.Exit(code, idx, label)))
    ctx.reload()
    if ctx.runner.dry_run:
        ctx.out.info(f"[dry-run] would write {ctx.paths.wg(f'px-{code}.conf')} and wx-{code}.conf, "
                     f"add its routing to {ctx.cfg.wg_conf} and check it")
        return
    e = ctx.snap.exit(code)
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


def cmd_remove(ctx, words: List[str]) -> None:
    """exit remove <code>: refused while clients use it; removes its tunnels,
    routing, firewall rules, WARP account and Proton config (and their copies)."""
    from . import ops
    code = words[0] if words else ""
    e = ctx.snap.exit(code) if code else None
    if e is None:
        raise WgtError(f"no exit city '{code}' (see: wgt exit list)")
    users = " ".join(c.name for c in ctx.snap.exit_clients(code))
    if users:
        raise WgtError(f"clients use exit '{code}': {users}; remove them first (wgt client remove <name>)")
    ops.take_lock(ctx)
    ctx.runner.step(f"Remove exit city '{code}' ({e.label}): its tunnels, routing, WARP account and Proton config",
                    f"wgt exit add {code} <proton.conf>")
    ops.confirm(ctx, f"Remove exit {code}?")
    down(ctx, code)
    if ops.wg_up(ctx):
        ctx.runner.sh_lines(rules.ipt_cmds(list(reversed(rules.ipt_rules_out(ctx.cfg, f"wx-{code}"))), "down"))
        ctx.runner.sh_lines(rules.exit_hook_cmds(ctx.cfg, ctx.snap, e, "down"))
    if store.state_get(ctx.snap.state, "UFW") == "1" and ctx.runner.ok(["sh", "-c", "command -v ufw"]):
        ctx.runner.run(["ufw", "route", "delete", "allow", "in", "on", ctx.cfg.WG_IF, "out", "on", f"wx-{code}"], check=False)
    ctx.files.write(ctx.paths.exit_db, 0o600, store.put_exit(store.read_text(ctx.paths.exit_db), code))
    ctx.reload()
    ops.write_wg_conf(ctx)
    for f in (ctx.paths.wg(f"px-{code}.conf"), ctx.paths.wg(f"wx-{code}.conf"), ctx.paths.wg("exits", f"{code}.proton.conf")):
        ctx.files.forget(f)
    ctx.files.remove_tree(ctx.paths.wg("exits", code))
    if not ctx.snap.exits:
        units.remove_timer(ctx, EXIT_WATCHDOG)
        ctx.runner.run(["systemctl", "daemon-reload"])


def cmd_test(ctx, words: List[str]) -> None:
    from . import checks, ops
    ops.take_lock(ctx)
    ctx.out.log("Exit city tests")
    res = checks.Results()
    run_tests(ctx, res, bring_up=True)
    checks.report(ctx, res)


def cmd_exit(ctx, words: List[str]) -> None:
    from .ops import require_setup
    require_setup(ctx)
    sub, rest = (words[0], words[1:]) if words else ("list", [])
    table = {"list": lambda c, w: print_list(c), "add": cmd_add, "remove": cmd_remove, "rm": cmd_remove, "test": cmd_test}
    fn = table.get(sub)
    if fn is None:
        raise WgtError("usage: wgt exit [list | add <code> <proton.conf|-> [--label NAME] | remove <code> | test]")
    fn(ctx, rest)


# --- checks ---

def tables_ok(ctx, e: store.Exit) -> bool:
    """Its tables have blackhole fallbacks and the fwmark rule points at Proton."""
    r = ctx.runner
    w, p, m = rules.exit_wtable(e), rules.exit_ptable(e), rules.exit_mark(e)
    if f"fwmark {m:#x} lookup {p}" not in live.rules(r):
        return False
    if "\nblackhole default" not in "\n" + live.table(r, "ip", p):
        return False
    return all("\nblackhole default" in "\n" + live.table(r, f, w) for f in ctx.cfg.families())


def client_rules_ok(ctx, e: store.Exit) -> bool:
    """One rule per client address."""
    w = rules.exit_wtable(e)
    n = len(ctx.snap.exit_clients(e.code))
    for f in ctx.cfg.families():
        found = sum(1 for line in live.rules(ctx.runner, f).splitlines() if re.search(rf"lookup {w}\b", line))
        if found != n:
            return False
    return True


def run_tests(ctx, res, bring_up: bool = False) -> None:
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
        act = active(ctx, e.code)
        started = False
        if bring_up and not is_up(ctx, e.code):
            up(ctx, e.code)
            started = True
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
            res.result("FAIL", f"exit {e.code}: down while {act} client(s) are active (the watchdog brings it up within 10s)")
        else:
            res.result("PASS", f"exit {e.code}: idle, no active clients (comes up when one connects; "
                               f"'wgt exit test' checks it live)")
    res.check("exit watchdog timer enabled and running",
              live.unit_enabled(r, f"{EXIT_WATCHDOG}.timer") and live.unit_active(r, f"{EXIT_WATCHDOG}.timer"))
