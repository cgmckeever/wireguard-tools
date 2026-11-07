"""`wgt test` (the PASS/FAIL checks), `wgt status` (everything running) and
`wgt summary` (one line per fact), plus the settings table setup's review
also shows."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional, Tuple

from . import clients, exits, host, live, ops, results, rules, store, ui
from .const import (
    DEVICE_SERVER,
    DEVICE_WATCHDOG,
    DIRECT_STOP_PRIO,
    EXIT_RULE_PRIO,
    EXIT_WATCHDOG,
    SETTING_ROWS,
    WARP_RULE_PRIO,
    WARP_WATCHDOG,
    WATCHDOG_STALE,
    device_ip,
)

if TYPE_CHECKING:
    from .ctx import Ctx

# --- settings table ---

def setting_src(ctx: Ctx, key: str, subnet_asked: bool = False) -> str:
    """Where a setting's value comes from."""
    src = ctx.cfg.sources.get(key, "")
    if src in ("flag", "env"):
        return src
    if key == "WG_SUBNET" and subnet_asked:
        return "entered"
    if ctx.cfg.saved.get(key):
        return "saved"
    if key in ("WG_SUBNET", "WG_SUBNET6"):
        return "random (new)"
    if key == "ENDPOINT":
        return "detected"
    return "default"


def running_value(ctx: Ctx, key: str) -> str:
    """What wg0/warp are actually running for a setting ("" if it can't be read)."""
    r, c = ctx.runner, ctx.cfg
    if key == "WG_PORT":
        return live.listen_port(r, c.WG_IF)
    if key == "WG_SUBNET":
        return live.addr_of(r, c.WG_IF, "-4")
    if key == "WG_SUBNET6":
        return live.addr_of(r, c.WG_IF, "-6")
    if key == "WG_MTU":
        return live.mtu(c.WG_IF)
    if key == "WARP_MTU":
        return live.mtu(c.WARP_IF)
    return ""


def expected_running(ctx: Ctx, key: str) -> str:
    """The running value a setting should have (none for settings that only live
    in client configs: name, endpoint, DNS, allowed IPs)."""
    c = ctx.cfg
    if key == "WG_SUBNET":
        return f"{c.server_ip}/{c.pfx}"
    if key == "WG_SUBNET6":
        return f"{c.p6}::1/64" if c.ipv6 else ""
    if key in ("WG_PORT", "WG_MTU", "WARP_MTU"):
        return c.values[key]
    return ""


@dataclass
class SettingsReport:
    lines: List[str] = field(default_factory=list)
    changes: List[str] = field(default_factory=list)   # "label  old -> new"
    changes_client_configs: bool = False
    pending: int = 0                                   # saved but not running yet


def settings_table(ctx: Ctx, running: bool = False, subnet_asked: bool = False) -> SettingsReport:
    """Each setting, its value and where it came from; with <running>, what
    wg0/warp run now (pending when it differs)."""
    rep = SettingsReport()
    for key, label, default in SETTING_ROWS:
        value = ctx.cfg.values.get(key, "")
        saved = ctx.cfg.saved.get(key, "")
        # A setting at its default is hidden, unless the saved value differed.
        if default and value == default and saved in ("", default):
            continue
        extra = ""
        if running:
            now, want = running_value(ctx, key), expected_running(ctx, key)
            if want or now:
                extra = f"running: {now or 'none'}"
                if now != want:
                    extra += "  (pending)"
                    rep.pending += 1
        line = f"    {label:<15} {value or '<not detected>':<24} {setting_src(ctx, key, subnet_asked):<8} {extra}"
        rep.lines.append(line.rstrip(" "))
        if saved and saved != value:
            rep.changes.append(f"{label:<15} {saved} -> {value}")
            if key in ("SERVER_NAME", "ENDPOINT", "WG_PORT", "WG_SUBNET", "WG_SUBNET6", "WG_DNS", "WG_MTU"):
                rep.changes_client_configs = True
    return rep


# --- test ---

class Tests:
    """The checks."""

    def __init__(self, ctx: Ctx, res: Optional[results.Results] = None):
        self.ctx = ctx
        self.runner = ctx.runner
        self.cfg = ctx.cfg
        self.res = res or results.Results(out=ctx.out.line)

    def warp_trace_on(self) -> bool:
        """Cloudflare's trace through the WARP tunnel says warp=on (or plus)."""
        return live.trace_field(live.trace(self.runner, "--interface", self.cfg.WARP_IF), "warp") in ("on", "plus")

    def warp6_trace_on(self) -> bool:
        """warp has no IPv6 route in the main table (Table = off), so route WARP's
        own address through RT_TABLE for the length of the check."""
        src = live.ipv6_global_addr(self.runner, self.cfg.WARP_IF)
        if not src:
            return False
        rule = ["from", src, "lookup", self.cfg.RT_TABLE, "priority", str(WARP_RULE_PRIO - 1)]
        self.runner.attempt(["ip", "-6", "rule", "add"] + rule)
        try:
            out = self.runner.query(["curl", "-6", "-fsS", "--max-time", "10", "--interface", src,
                                "https://[2606:4700:4700::1111]/cdn-cgi/trace"])
        finally:
            self.runner.attempt(["ip", "-6", "rule", "del"] + rule)
        return live.is_warp_trace(out)

    def default_route_kept(self) -> bool:
        """The main table's default route is the one recorded at install (SSH depends on it)."""
        rec = store.state_get(self.ctx.snap.state, "DEFAULT_ROUTE")
        return bool(rec) and host.route_key(self.ctx.facts.default_route) == host.route_key(rec)

    def warp_rule_ok(self, family: str) -> bool:
        """The subnet's policy rule into RT_TABLE is there exactly once."""
        needle = f"from {self.cfg.family_net(family)} lookup {self.cfg.RT_TABLE}"
        return sum(1 for line in live.rules(self.runner, family).splitlines() if needle in line) == 1

    def rt_table_ok(self, family: str) -> bool:
        """RT_TABLE has the warp default, the blackhole fallback and the client subnet."""
        t = live.table(self.runner, family, self.cfg.RT_TABLE)
        net = re.escape(self.cfg.family_net(family))
        return bool(re.search(rf"^default dev {re.escape(self.cfg.WARP_IF)}( |$)", t, re.M)
                    and re.search(r"^blackhole default( |$)", t, re.M)
                    and re.search(rf"^{net} dev {re.escape(self.cfg.WG_IF)}( |$)", t, re.M))

    def direct_rules_ok(self, family: str) -> bool:
        """Each WARP-off client without a jump: its main-table rule and the
        blackhole behind it, once each, and no others of either kind."""
        out = live.rules(self.runner, family).splitlines()
        want = set()
        for c in self.ctx.snap.direct_clients():
            want.add(self.cfg.ip6_of(c.ip) if family == "ip -6" else c.ip)
        patterns = (rf"^{EXIT_RULE_PRIO}:\s+from (\S+) lookup main\b",
                    rf"^{DIRECT_STOP_PRIO}:\s+from (\S+) blackhole\b")
        for pattern in patterns:
            found = [m.group(1) for m in (re.match(pattern, line) for line in out) if m]
            if sorted(found) != sorted(want):
                return False
        return True

    def wg0_drift(self) -> List[str]:
        """What wg0's PostUp sets up that isn't live (a wg0 started with an
        older config, or rules removed by hand): empty when it all matches."""
        c, out = self.cfg, []
        for f in c.families():
            if not self.warp_rule_ok(f):
                out.append(f"{f} WARP rule")
            t = live.table(self.runner, f, c.RT_TABLE)
            net = re.escape(c.family_net(f))
            if not (re.search(r"^blackhole default( |$)", t, re.M)
                    and re.search(rf"^{net} dev {re.escape(c.WG_IF)}( |$)", t, re.M)):
                out.append(f"{f} table {c.RT_TABLE}")
            if not self.direct_rules_ok(f):
                out.append(f"{f} plain host config rules")
        if not self.device_net_ok():
            out.append("static device addresses")
        for x, t, ch, rule in rules.ipt_rules(c, self.ctx.pub_if, self.ctx.snap.exits):
            if live.ipt_count(self.runner, x, t, ch, rule) != 1:
                out.append(f"{x} {t} {ch} {rule}")
        for e in self.ctx.snap.exits:
            if not exits.client_rules_ok(self.ctx, e):
                out.append(f"jump {e.code} config rules")
        return out

    def device_net_ok(self) -> bool:
        """wg0 holds the server's static address, and an address with no config
        in use is unreachable instead of taking a way out."""
        c = self.cfg
        addrs = self.runner.query(["ip", "-4", "-o", "addr", "show", "dev", c.WG_IF])
        return f" {device_ip(DEVICE_SERVER)}/" in addrs and rules.device_rule_text(c) in live.rules(self.runner, "ip")

    def boot_order_ok(self) -> bool:
        """wg0's unit starts after warp's (and wants it), via setup's drop-in."""
        unit = f"wg-quick@{self.cfg.WG_IF}"
        want = f" wg-quick@{self.cfg.WARP_IF}.service "
        return (want in f" {live.unit_prop(self.runner, unit, 'After')} "
                and want in f" {live.unit_prop(self.runner, unit, 'Wants')} ")

    def run(self, scope: str = "all") -> results.Results:
        """Run the checks ("warp": only those WARP itself needs)."""
        r, c, res = self.runner, self.cfg, self.res
        self.res.check(f"{c.WARP_IF}: curl --interface {c.WARP_IF} shows warp=on", self.warp_trace_on())
        if c.ipv6:
            self.res.check(f"{c.WARP_IF}: IPv6 through {c.WARP_IF} shows warp=on", self.warp6_trace_on())
        self.res.check(f"{c.WARP_IF}: handshake within {WATCHDOG_STALE}s",
                        live.hs_recent(r, c.WARP_IF, WATCHDOG_STALE, self.ctx.clock()))
        res.check("server's own traffic is not tunneled (plain curl shows warp=off)",
                  live.trace_field(live.trace(r), "warp") == "off")
        res.check(f"default route unchanged since install ({store.state_get(self.ctx.snap.state, 'DEFAULT_ROUTE')})",
                  self.default_route_kept())
        if scope == "warp":
            return res
        if store.read_text(c.wg_conf) is None:
            res.result("SKIP", f"{c.WG_IF} checks (the WireGuard server isn't set up yet)")
            return res
        res.check(f"{c.WG_IF} listening on udp/{c.WG_PORT}", live.listen_port(r, c.WG_IF) == c.WG_PORT)
        res.check("net.ipv4.ip_forward = 1", live.sysctl(r, "net.ipv4.ip_forward") == "1")
        if c.ipv6:
            res.check("net.ipv6.conf.all.forwarding = 1", live.sysctl(r, "net.ipv6.conf.all.forwarding") == "1")
        for f in c.families():
            net = c.family_net(f)
            res.check(f"{f} rule: from {net} lookup {c.RT_TABLE} (exactly once)", self.warp_rule_ok(f))
            self.res.check(f"{f} table {c.RT_TABLE}: default via {c.WARP_IF}, blackhole fallback, {net} via {c.WG_IF}",
                            self.rt_table_ok(f))
            direct = len(self.ctx.snap.direct_clients())
            res.check(f"{f} rules: {direct} plain host config(s) leave via this server's IP "
                      "(main, then blackhole; no stale ones)", self.direct_rules_ok(f))
        res.check(f"static addresses: {device_ip(DEVICE_SERVER)} on {c.WG_IF}; one with no config in use "
                  "is unreachable", self.device_net_ok())
        orphans = [x.name for x in self.ctx.snap.clients if x.exit and self.ctx.snap.exit(x.exit) is None]
        res.check("every config's jump exists" + (f" (missing for: {' '.join(orphans)})" if orphans else ""),
                  not orphans)
        for x, t, ch, rule in rules.ipt_rules(c, self.ctx.pub_if, self.ctx.snap.exits):
            res.check(f"exactly once: {x} {t} {ch} {rule}", live.ipt_count(r, x, t, ch, rule) == 1)
        res.check(f"wg-quick@{c.WG_IF} enabled at boot", live.unit_enabled(r, f"wg-quick@{c.WG_IF}"))
        self.res.check(f"wg-quick@{c.WARP_IF} enabled at boot", live.unit_enabled(r, f"wg-quick@{c.WARP_IF}"))
        res.check(f"wg-quick@{c.WG_IF} ordered after (and wants) wg-quick@{c.WARP_IF}", self.boot_order_ok())
        res.check("WARP watchdog timer enabled and running",
                  live.timer_on(r, WARP_WATCHDOG))
        res.check("device watchdog timer enabled and running", live.timer_on(r, DEVICE_WATCHDOG))
        if self.ctx.snap.exits:
            exits.run_tests(self.ctx, res, bring_up=False)
        if any(ts > 0 for ts in live.handshakes(r, c.WG_IF).values()):
            res.result("PASS", "at least one device has connected")
        else:
            res.result("WARN", "no device has connected yet")
        return res


def watchdogs(ctx: Ctx) -> List[str]:
    """The watchdogs that should be running (the jump one only with jumps)."""
    return [WARP_WATCHDOG, DEVICE_WATCHDOG] + ([EXIT_WATCHDOG] if ctx.snap.exits else [])


def cmd_test(ctx: Ctx, words: List[str]) -> int:
    """All checks (WARP, wg0, rules, units, jumps)."""
    ops.no_more_words(words, "wgt test")
    ops.require_setup(ctx)
    ctx.out.log("Tests")
    return results.report(ctx, Tests(ctx).run("all"))


# --- status ---

def cmd_status(ctx: Ctx, words: List[str]) -> None:
    """Settings (and whether they're running), interfaces, watchdogs, jumps,
    clients, routes and rules."""
    ops.no_more_words(words, "wgt status")
    ops.require_setup(ctx)
    r, c, out = ctx.runner, ctx.cfg, ctx.out
    out.log(f"Settings (saved in {ctx.paths.state})")
    rep = settings_table(ctx, running=True)
    for line in rep.lines:
        ctx.out.line(line)
    if rep.pending:
        out.warn(f"{rep.pending} saved setting(s) pending (not running yet); apply with: wgt setup")
    out.log(f"WARP (warp.* configs leave via {c.WARP_IF}, the rest straight out)")
    out.log("Interfaces")
    for ifc in (c.WARP_IF, c.WG_IF):
        active, enabled = live.unit_state(r, f"wg-quick@{ifc}")
        if live.if_exists(r, ifc):
            brief = r.query(["ip", "-br", "addr", "show", "dev", ifc]).rstrip("\n")
            out.info(f"{brief}  [{active}, {enabled or 'disabled'}]")
        else:
            out.info(f"{ifc}: absent  [{enabled or 'disabled'}]")
    for name in watchdogs(ctx):
        out.info(f"watchdog: {name}.timer {live.unit_state(r, f'{name}.timer')[0]}")
    out.info(f"last WARP port change: {live.last_event(r, WARP_WATCHDOG) or 'never'}")
    out.log("WARP handshake")
    if live.if_exists(r, c.WARP_IF):
        out.info(f"{c.WARP_IF}: {ui.ago(live.newest_hs(r, c.WARP_IF))}")
    if ctx.snap.exits:
        out.log("Jumps")
        exits.print_list(ctx)
    out.log("Clients")
    clients.print_clients(ctx, prefix="    ", numbered=False)
    if not ctx.snap.clients:
        out.info("none yet (add one: wgt client add <device>)")
    out.log("Routes")
    out.info(f"main default: {ctx.facts.default_route}  (recorded: {store.state_get(ctx.snap.state, 'DEFAULT_ROUTE')})")
    for f in c.families():
        found = [line for line in live.rules(r, f).splitlines() if f"lookup {c.RT_TABLE}" in line]
        if found:
            for line in found:
                ctx.out.info(f"{f} rule: {line}")
        else:
            out.info(f"{f} rule: MISSING (run: wgt setup)")
        for line in live.table(r, f, c.RT_TABLE).splitlines():
            ctx.out.info(f"{f} table {c.RT_TABLE}: {line}")
    out.log("Rules")
    for x, t, ch, rule in rules.ipt_rules(c, ctx.pub_if, ctx.snap.exits):
        present = live.ipt_present(r, (x, t, ch, rule))
        out.info(f"{'present' if present else 'missing'}  {x} -t {t} {ch} {rule}")
    out.info(f"ip_forward = {live.sysctl(r, 'net.ipv4.ip_forward')}")
    if c.ipv6:
        out.info(f"IPv6: clients in {c.WG_SUBNET6}, ipv6 forwarding = {live.sysctl(r, 'net.ipv6.conf.all.forwarding')}")
    else:
        why = "disabled" if c.WG_SUBNET6 == "off" else "kernel IPv6 disabled"
        out.info("IPv6: off" + (f" ({why})" if c.WG_SUBNET6 else ""))


# --- summary (wgt summary; wgt-ctl summary shows every server's) ---

SUMMARY_FMT = "    {:<11} {}"


def cmd_summary(ctx: Ctx, words: List[str]) -> None:
    """This server at a glance, one fact per line under its name: WARP,
    tunnels, settings, route, firewall, watchdog, jumps, clients.
    Each line says what is wrong when something is (details: wgt status)."""
    ops.no_more_words(words, "wgt summary")
    if not ops.is_set_up(ctx):
        ctx.out.line(ctx.facts.hostname.split(".")[0])
        ctx.out.line(SUMMARY_FMT.format("state", "not set up (wgt setup)"))
        return
    ctx.out.line(f"{ctx.cfg.SERVER_NAME}  ({ctx.cfg.values.get('ENDPOINT') or 'no endpoint saved'})")
    for label, text in summary_lines(ctx):
        ctx.out.line(SUMMARY_FMT.format(label, text))


def summary_lines(ctx: Ctx) -> List[Tuple[str, str]]:
    """(label, text) pairs for cmd_summary; a label repeats as "" on continuation lines."""
    r, c = ctx.runner, ctx.cfg
    now = ctx.clock()
    lines: List[Tuple[str, str]] = []

    if live.if_exists(r, c.WARP_IF):
        warp_hs = live.newest_hs(r, c.WARP_IF)
        fresh = warp_hs > 0 and now - warp_hs <= WATCHDOG_STALE
        warp = f"handshake {ui.ago(warp_hs, now)}" + ("" if fresh else "  <- STALE")
    else:
        warp = "tunnel DOWN"
    lines.append(("WARP", warp))

    problems = []
    for ifc in (c.WG_IF, c.WARP_IF):
        at_boot = live.unit_state(r, f"wg-quick@{ifc}")[1]
        if not live.if_exists(r, ifc):
            problems.append(f"{ifc} DOWN")
        elif at_boot != "enabled":
            problems.append(f"{ifc} not enabled at boot")
    lines.append(("tunnels", ", ".join(problems) if problems
                  else f"{c.WG_IF} and {c.WARP_IF} up, enabled at boot"))

    pending = settings_table(ctx, running=True).pending
    lines.append(("settings", f"{pending} saved but not running (apply: wgt setup)" if pending
                  else "running as saved"))

    recorded = store.state_get(ctx.snap.state, "DEFAULT_ROUTE")
    lines.append(("route", "default route unchanged since install" if Tests(ctx).default_route_kept()
                  else f"default route CHANGED (was: {recorded}; now: {ctx.facts.default_route})"))

    wanted = rules.ipt_rules(c, ctx.pub_if, ctx.snap.exits)
    missing = sum(1 for rule in wanted if not live.ipt_present(r, rule))
    lines.append(("firewall", f"all {len(wanted)} rules present" if not missing
                  else f"{missing} of {len(wanted)} rules MISSING (wgt status)"))

    stopped = [name for name in watchdogs(ctx) if live.unit_state(r, f"{name}.timer")[0] != "active"]
    lines.append(("watchdog", f"{'NOT RUNNING: ' + ' '.join(stopped) if stopped else 'running'}; "
                              f"last WARP port change: {live.last_event(r, WARP_WATCHDOG) or 'never'}"))

    if not ctx.snap.exits:
        lines.append(("jumps", "none"))
    for i, e in enumerate(ctx.snap.exits):
        users = len(ctx.snap.exit_clients(e.code))
        st = exits.state(ctx, e)
        state = f"{st}, {exits.active_count(ctx, e.code)} active" if st != "idle" else st
        lines.append(("jumps" if i == 0 else "", f"{e.code} ({e.label}): {state}; {users} client(s)"))

    hs = live.handshakes(r, c.WG_IF)
    in_use = [cl for cl in ctx.snap.clients if not ctx.snap.is_disabled(cl.name)]
    recent = [cl for cl in in_use if hs.get(cl.pub, 0) > 0 and now - hs[cl.pub] <= 3600]
    text = f"{len(ctx.snap.clients)}"
    if ctx.snap.clients:
        text += f": {len(recent)} seen in the last hour"
        disabled = len(ctx.snap.clients) - len(in_use)
        if disabled:
            text += f", {disabled} disabled"
    else:
        text += " (add one: wgt client add <device>)"
    lines.append(("clients", text))
    newest = max(in_use, key=lambda cl: hs.get(cl.pub, 0), default=None)
    if newest is not None and hs.get(newest.pub, 0):
        lines.append(("", f"newest: {store.device_of(newest.name)} via {clients.exit_label(ctx, newest.name)}, "
                          f"{ui.ago(hs[newest.pub], now)}"))
    return lines
