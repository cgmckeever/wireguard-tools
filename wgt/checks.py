"""`wgt test` (the PASS/FAIL checks) and `wgt status` (what's running), plus
the settings table setup's review also shows."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from . import exits, host, live, rules, store, ui
from .ops import require_setup
from .const import EXIT_WATCHDOG, SETTING_ROWS, WATCHDOG, WATCHDOG_STALE
from .errors import WgtError


# --- settings table ---

def setting_src(ctx, key: str, subnet_asked: bool = False) -> str:
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


def running_value(ctx, key: str) -> str:
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
    if key == "WARP":
        return "on" if f"from {c.WG_SUBNET} lookup {c.RT_TABLE}" in live.rules(r) else "off"
    return ""


def expected_running(ctx, key: str) -> str:
    """The running value a setting should have (none for settings that only live
    in client configs: name, endpoint, DNS, allowed IPs)."""
    c = ctx.cfg
    if key == "WG_SUBNET":
        return f"{c.server_ip}/{c.pfx}"
    if key == "WG_SUBNET6":
        return f"{c.p6}::1/64" if c.ipv6 else ""
    if key in ("WG_PORT", "WG_MTU", "WARP_MTU", "WARP"):
        return c.values[key]
    return ""


@dataclass
class SettingsReport:
    lines: List[str] = field(default_factory=list)
    changes: List[str] = field(default_factory=list)   # "label  old -> new"
    changes_client_configs: bool = False
    pending: int = 0                                   # saved but not running yet


def settings_table(ctx, running: bool = False, subnet_asked: bool = False) -> SettingsReport:
    """Each setting, its value and where it came from; with <running>, what
    wg0/warp run now (pending when it differs)."""
    rep = SettingsReport()
    for key, label, default in SETTING_ROWS:
        value = ctx.cfg.values.get(key, "")
        saved = ctx.cfg.saved.get(key, "")
        if default and value == default and (saved or default) == default:
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

@dataclass
class Results:
    passed: int = 0
    failed: int = 0
    warned: int = 0
    out: Callable[[str], None] = print

    def result(self, kind: str, desc: str) -> None:
        self.out(f"  {kind:<4} {desc}")
        if kind == "PASS":
            self.passed += 1
        elif kind == "FAIL":
            self.failed += 1
        elif kind == "WARN":
            self.warned += 1

    def check(self, desc: str, ok: bool) -> None:
        self.result("PASS" if ok else "FAIL", desc)


class Tests:
    """The checks. WARP checks only fail the run while WARP is on (or when
    testing WARP itself)."""

    def __init__(self, ctx, results: Optional[Results] = None):
        self.ctx = ctx
        self.r = ctx.runner
        self.c = ctx.cfg
        self.res = results or Results()
        self.scope = "all"

    def check_warp(self, desc: str, ok: bool) -> None:
        if ok:
            self.res.result("PASS", desc)
        elif self.c.WARP == "on" or self.scope == "warp":
            self.res.result("FAIL", desc)
        else:
            self.res.result("WARN", f"{desc} (WARP is off)")

    def warp_on(self) -> bool:
        return live.trace_field(live.trace(self.r, "--interface", self.c.WARP_IF), "warp") in ("on", "plus")

    def warp6_on(self) -> bool:
        """warp has no IPv6 route in the main table (Table = off), so route WARP's
        own address through RT_TABLE for the length of the check."""
        src = live.ipv6_global_addr(self.r, self.c.WARP_IF)
        if not src:
            return False
        prio = str(9999)
        rule = ["ip", "-6", "rule", "{}", "from", src, "lookup", self.c.RT_TABLE, "priority", prio]
        self.r.ok([x if x != "{}" else "add" for x in rule])
        out = self.r.query(["curl", "-6", "-fsS", "--max-time", "10", "--interface", src,
                            "https://[2606:4700:4700::1111]/cdn-cgi/trace"])
        self.r.ok([x if x != "{}" else "del" for x in rule])
        return live.is_warp_trace(out)

    def default_route_kept(self) -> bool:
        rec = store.state_get(self.ctx.snap.state, "DEFAULT_ROUTE")
        return bool(rec) and host.route_key(self.ctx.facts.default_route) == host.route_key(rec)

    def ip_rule(self, family: str) -> bool:
        want = 1 if self.c.WARP == "on" else 0
        needle = f"from {self.c.family_net(family)} lookup {self.c.RT_TABLE}"
        return sum(1 for line in live.rules(self.r, family).splitlines() if needle in line) == want

    def table(self, family: str) -> bool:
        import re
        t = live.table(self.r, family, self.c.RT_TABLE)
        net = re.escape(self.c.family_net(family))
        return bool(re.search(rf"^default dev {re.escape(self.c.WARP_IF)}( |$)", t, re.M)
                    and re.search(r"^blackhole default( |$)", t, re.M)
                    and re.search(rf"^{net} dev {re.escape(self.c.WG_IF)}( |$)", t, re.M))

    def ordering(self) -> bool:
        unit = f"wg-quick@{self.c.WG_IF}"
        want = f" wg-quick@{self.c.WARP_IF}.service "
        return (want in f" {live.unit_prop(self.r, unit, 'After')} "
                and want in f" {live.unit_prop(self.r, unit, 'Wants')} ")

    def run(self, scope: str = "all") -> Results:
        r, c, res = self.r, self.c, self.res
        self.scope = scope
        self.check_warp(f"{c.WARP_IF}: curl --interface {c.WARP_IF} shows warp=on", self.warp_on())
        if c.ipv6:
            self.check_warp(f"{c.WARP_IF}: IPv6 through {c.WARP_IF} shows warp=on", self.warp6_on())
        self.check_warp(f"{c.WARP_IF}: handshake within the last 3 minutes",
                        live.hs_recent(r, c.WARP_IF, WATCHDOG_STALE))
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
            if c.WARP == "on":
                res.check(f"{f} rule: from {net} lookup {c.RT_TABLE} (exactly once; WARP on)", self.ip_rule(f))
            else:
                res.check(f"{f} rule: none from {net} (WARP off)", self.ip_rule(f))
            self.check_warp(f"{f} table {c.RT_TABLE}: default via {c.WARP_IF}, blackhole fallback, {net} via {c.WG_IF}",
                            self.table(f))
        for x, t, ch, rule in rules.ipt_rules(c, self.ctx.pub_if, self.ctx.snap.exits):
            res.check(f"exactly once: {x} {t} {ch} {rule}", live.ipt_count(r, x, t, ch, rule) == 1)
        res.check(f"wg-quick@{c.WG_IF} enabled at boot", live.unit_enabled(r, f"wg-quick@{c.WG_IF}"))
        self.check_warp(f"wg-quick@{c.WARP_IF} enabled at boot", live.unit_enabled(r, f"wg-quick@{c.WARP_IF}"))
        res.check(f"wg-quick@{c.WG_IF} ordered after (and wants) wg-quick@{c.WARP_IF}", self.ordering())
        res.check("WARP watchdog timer enabled and running",
                  live.unit_enabled(r, f"{WATCHDOG}.timer") and live.unit_active(r, f"{WATCHDOG}.timer"))
        if self.ctx.snap.exits:
            exits.run_tests(self.ctx, res, bring_up=False)
        if any(ts > 0 for ts in live.handshakes(r, c.WG_IF).values()):
            res.result("PASS", "at least one client handshake")
        else:
            res.result("WARN", "no client has connected yet")
        return res


def report(ctx, res: Results) -> None:
    """Print the totals; raise (exit 1) on any FAIL."""
    print("")
    ctx.out.info(f"{res.passed} passed, {res.failed} failed, {res.warned} warnings")
    if res.failed:
        raise SystemExit(1)


def cmd_test(ctx, words: List[str]) -> None:
    require_setup(ctx)
    ctx.out.log(f"Tests (WARP {ctx.cfg.WARP})")
    report(ctx, Tests(ctx).run("all"))


# --- status ---

def cmd_status(ctx, words: List[str]) -> None:
    """Settings (and whether they're running), interfaces, watchdogs, exit
    cities, clients, routes and rules."""
    require_setup(ctx)
    r, c, out = ctx.runner, ctx.cfg, ctx.out
    out.log(f"Settings (saved in {ctx.paths.state})")
    rep = settings_table(ctx, running=True)
    for line in rep.lines:
        print(line)
    if rep.pending:
        out.warn(f"{rep.pending} saved setting(s) pending (not running yet); apply with: wgt setup")
    out.log(f"WARP {c.WARP} (client traffic leaves via {c.WARP_IF if c.WARP == 'on' else ctx.pub_if})")
    out.log("Interfaces")
    for ifc in (c.WARP_IF, c.WG_IF):
        active, enabled = live.unit_state(r, f"wg-quick@{ifc}")
        if live.if_exists(r, ifc):
            out.info(f"{r.query(['ip', '-br', 'addr', 'show', 'dev', ifc]).rstrip(chr(10))}  [{active}, {enabled or 'disabled'}]")
        else:
            out.info(f"{ifc}: absent  [{enabled or 'disabled'}]")
    last = r.query(["journalctl", "-t", WATCHDOG, "-n", "1", "-o", "short-iso", "--no-pager", "-q"]).strip()[:25]
    out.info(f"watchdog: {WATCHDOG}.timer {live.unit_state(r, WATCHDOG + '.timer')[0]}; last port change: {last or 'never'}")
    out.log("WARP handshake")
    if live.if_exists(r, c.WARP_IF):
        out.info(f"{c.WARP_IF}: {ui.ago(live.newest_hs(r, c.WARP_IF))}")
    if ctx.snap.exits:
        out.log("Exit cities")
        exits.print_list(ctx)
    out.log("Clients")
    print_clients(ctx, prefix="    ", numbered=False)
    if not ctx.snap.clients:
        out.info("none yet (add one: wgt client add <name>)")
    out.log("Routes")
    out.info(f"main default: {ctx.facts.default_route}  (recorded: {store.state_get(ctx.snap.state, 'DEFAULT_ROUTE')})")
    for f in c.families():
        found = [line for line in live.rules(r, f).splitlines() if f"lookup {c.RT_TABLE}" in line]
        if found:
            for line in found:
                print(f"    {f} rule: {line}")
        else:
            out.info(f"{f} rule: none (WARP off)")
        for line in live.table(r, f, c.RT_TABLE).splitlines():
            print(f"    {f} table {c.RT_TABLE}: {line}")
    out.log("Rules")
    for x, t, ch, rule in rules.ipt_rules(c, ctx.pub_if, ctx.snap.exits):
        present = r.ok([x, "-t", t, "-C", ch] + rule.split())
        out.info(f"{'present' if present else 'missing'}  {x} -t {t} {ch} {rule}")
    out.info(f"ip_forward = {live.sysctl(r, 'net.ipv4.ip_forward')}")
    if c.ipv6:
        out.info(f"IPv6: clients in {c.WG_SUBNET6}, ipv6 forwarding = {live.sysctl(r, 'net.ipv6.conf.all.forwarding')}")
    else:
        why = "disabled" if c.WG_SUBNET6 == "off" else "kernel IPv6 disabled"
        out.info("IPv6: off" + (f" ({why})" if c.WG_SUBNET6 else ""))


# --- the clients table (status, client list) ---

CLIENT_FMT = "{:<20} {:<6} {:<15} {:<14} {:<12} {}"


def client_rows(ctx) -> List[Tuple[str, str, str, str, str, str]]:
    """(name, exit, address, last seen, rx/tx MiB, allowed IPs) per client."""
    hs = live.handshakes(ctx.runner, ctx.cfg.WG_IF)
    tx = live.transfers(ctx.runner, ctx.cfg.WG_IF)
    now = time.time()
    rows = []
    for cl in ctx.snap.clients:
        seen = "disabled" if ctx.snap.is_disabled(cl.name) else ui.ago(hs.get(cl.pub, 0), now)
        rx_b, tx_b = tx.get(cl.pub, (0, 0))
        rows.append((cl.name, exit_label(ctx, cl.name), cl.ip, seen,
                     f"{rx_b // 1048576}/{tx_b // 1048576}", cl.allowed.replace(",", ", ")))
    return rows


def exit_label(ctx, name: str) -> str:
    """Where a client exits, for tables: its exit code, or warp/direct (the default)."""
    code = store.client_exit(name)
    if code:
        return code
    return "warp" if ctx.cfg.WARP == "on" else "direct"


def print_clients(ctx, prefix: str = "", numbered: bool = True) -> List[str]:
    """Print the clients table; returns the names in order."""
    head = ("NAME", "EXIT", "ADDRESS", "LAST SEEN", "RX/TX MiB", "ALLOWED IPS")
    if numbered:
        print(f"{prefix}{'#':>3} " + CLIENT_FMT.format(*head))
    else:
        print(prefix + CLIENT_FMT.format(*head))
    names = []
    for i, row in enumerate(client_rows(ctx), 1):
        names.append(row[0])
        line = CLIENT_FMT.format(*row)
        print(f"{prefix}{i:>3} {line}" if numbered else prefix + line)
    return names
