"""Exit cities: clients named <code>.<name> leave through a Proton VPN city
with WARP inside it. This module holds the read-only parts (list, activity,
checks); adding, removing and bringing exits up and down follow in the port."""

from __future__ import annotations

import time
from typing import List

from . import live, rules, store
from .const import EXIT_IDLE, EXIT_WATCHDOG, WATCHDOG_STALE


def is_up(ctx, code: str) -> bool:
    """Both tunnels of an exit are up."""
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


def cmd_exit(ctx, words: List[str]) -> None:
    from .ops import require_setup
    require_setup(ctx)
    sub = words[0] if words else "list"
    if sub == "list":
        print_list(ctx)
        return
    from .errors import WgtError
    raise WgtError(f"'wgt exit {sub}' isn't ported yet; use wg-setup.sh exit {sub}")


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
    import re
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
        if bring_up and not is_up(ctx, e.code):
            from .errors import WgtError
            raise WgtError("bringing exits up for a check isn't ported yet; use wg-setup.sh exit test")
        if is_up(ctx, e.code):
            res.check(f"exit {e.code}: Proton tunnel px-{e.code} handshake within {WATCHDOG_STALE}s",
                      live.hs_recent(r, f"px-{e.code}", WATCHDOG_STALE))
            res.check(f"exit {e.code}: WARP tunnel wx-{e.code} handshake within {WATCHDOG_STALE}s",
                      live.hs_recent(r, f"wx-{e.code}", WATCHDOG_STALE))
            tr = live.trace_summary(live.trace(r, "--interface", f"wx-{e.code}"))
            if live.is_warp_trace(tr):
                res.result("PASS", f"exit {e.code}: traffic exits through WARP ({tr})")
            else:
                res.result("FAIL", f"exit {e.code}: no WARP trace through wx-{e.code} ({tr or 'no answer'})")
        elif act > 0:
            res.result("FAIL", f"exit {e.code}: down while {act} client(s) are active (the watchdog brings it up within 10s)")
        else:
            res.result("PASS", f"exit {e.code}: idle, no active clients (comes up when one connects; "
                               f"'wgt exit test' checks it live)")
    res.check("exit watchdog timer enabled and running",
              live.unit_enabled(r, f"{EXIT_WATCHDOG}.timer") and live.unit_active(r, f"{EXIT_WATCHDOG}.timer"))
