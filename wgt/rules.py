"""Routing, NAT and firewall rules, as shell lines: one source for wg0's
PostUp/PostDown, rollback, live changes and the tests.

How it fits together (see README "How it works"):
- WARP switch: RT_TABLE always holds "default dev warp" with a blackhole
  fallback; WARP on adds one policy rule per family, `from <subnet> lookup
  RT_TABLE`, WARP off removes it.
- Exit city <code> with index n: client table 200+n (default dev wx-<code>,
  blackhole fallback) chosen by one rule per client address; Proton table
  230+n (default dev px-<code>, blackhole) chosen by the fwmark of the city's
  WARP tunnel. All of it lives in wg0's hooks, so a city that is down blocks
  its clients instead of leaking them.

Every line is idempotent, and "down" is the exact inverse of "up" in reverse
order. The text matches the bash version byte for byte (wg0.conf embeds it)."""

from __future__ import annotations

from typing import Iterable, List, Sequence, Tuple

from .config import Config
from .const import (BLACKHOLE_METRIC, EXIT_MARK_BASE, EXIT_MARK_PRIO, EXIT_PTABLE_BASE,
                    EXIT_RULE_PRIO, EXIT_TABLE_BASE, RULE_PRIO)
from .store import Client, Exit, Snapshot

IptRule = Tuple[str, str, str, str]   # command, table, chain, rule


def exit_wtable(e: Exit) -> int:
    return EXIT_TABLE_BASE + e.idx


def exit_ptable(e: Exit) -> int:
    return EXIT_PTABLE_BASE + e.idx


def exit_mark(e: Exit) -> int:
    return EXIT_MARK_BASE + e.idx


def out_ifs(cfg: Config, pub_if: str, exits: Sequence[Exit]) -> List[str]:
    """Every way out: WARP, the public interface, each exit city's WARP tunnel."""
    return [cfg.WARP_IF, pub_if] + [f"wx-{e.code}" for e in exits]


def rule_spec(cfg: Config, family: str) -> str:
    return f"from {cfg.family_net(family)} lookup {cfg.RT_TABLE} priority {RULE_PRIO}"


def warp_route(cfg: Config) -> List[str]:
    """RT_TABLE's default route(s) out through warp (in wg0's PostUp and warp.conf's)."""
    return [f"{f} route replace default dev {cfg.WARP_IF} table {cfg.RT_TABLE}" for f in cfg.families()]


def ipt_lines(cfg: Config, cmd: str, out: str) -> List[IptRule]:
    """The NAT/forwarding rules for one family (iptables|ip6tables) and way out."""
    net = cfg.family_net(cmd)
    wg = cfg.WG_IF
    return [
        (cmd, "nat", "POSTROUTING", f"-s {net} -o {out} -j MASQUERADE"),
        (cmd, "filter", "FORWARD", f"-i {wg} -o {out} -j ACCEPT"),
        (cmd, "filter", "FORWARD", f"-i {out} -o {wg} -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"),
        (cmd, "mangle", "FORWARD", f"-o {out} -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu"),
    ]


def ipt_rules(cfg: Config, pub_if: str, exits: Sequence[Exit]) -> List[IptRule]:
    """Firewall rules for every way out (each matches only its own -o, so all
    can stay in place), plus the WireGuard port. Inserted (-I) so ufw/Docker
    DROP policies can't shadow them."""
    out: List[IptRule] = []
    for cmd in ("iptables", "ip6tables"):
        if cmd == "ip6tables" and not cfg.ipv6:
            continue
        for o in out_ifs(cfg, pub_if, exits):
            out += ipt_lines(cfg, cmd, o)
    out.append(("iptables", "filter", "INPUT", f"-p udp --dport {cfg.WG_PORT} -j ACCEPT"))
    return out


def ipt_rules_out(cfg: Config, out: str) -> List[IptRule]:
    """The NAT/forwarding rules for one way out (both families)."""
    rules = ipt_lines(cfg, "iptables", out)
    if cfg.ipv6:
        rules += ipt_lines(cfg, "ip6tables", out)
    return rules


def ipt_cmds(rules: Iterable[IptRule], direction: str) -> List[str]:
    """Firewall rules as idempotent shell lines that add (up) or remove (down) them."""
    if direction == "up":
        return [f"{x} -t {t} -C {c} {r} 2>/dev/null || {x} -t {t} -I {c} {r}" for x, t, c, r in rules]
    return [f"while {x} -t {t} -C {c} {r} 2>/dev/null; do {x} -t {t} -D {c} {r} || break; done"
            for x, t, c, r in rules]


def warp_rule_cmds(cfg: Config, on: bool) -> List[str]:
    """The WARP switch: add (on) or remove (off) the policy rules sending client
    traffic to RT_TABLE."""
    out = []
    for f in cfg.families():
        spec = rule_spec(cfg, f)
        line = f"while {f} rule del {spec} 2>/dev/null; do :; done"
        out.append(f"{line}; {f} rule add {spec}" if on else line)
    return out


def exit_client_cmds(cfg: Config, snap: Snapshot, client: Client, direction: str) -> List[str]:
    """Policy rules sending one client's addresses (ip, ip6) to its exit's table."""
    e = snap.exit(client.exit) if client.exit else None
    if e is None or not client.ip:
        return []
    table = exit_wtable(e)
    out = []
    for f in cfg.families():
        a = f"{cfg.ip6_of(client.ip)}/128" if f == "ip -6" else f"{client.ip}/32"
        out.append(f"while {f} rule del from {a} lookup {table} priority {EXIT_RULE_PRIO} 2>/dev/null; do :; done")
        if direction == "up":
            out.append(f"{f} rule add from {a} lookup {table} priority {EXIT_RULE_PRIO}")
    return out


def exit_hook_cmds(cfg: Config, snap: Snapshot, e: Exit, direction: str) -> List[str]:
    """One exit's part of wg0's hooks: its tables with blackhole fallbacks, the
    fwmark rule sending its WARP tunnel's packets through Proton, and its
    clients' rules (its firewall rules come with ipt_rules)."""
    w, p, m = exit_wtable(e), exit_ptable(e), exit_mark(e)
    out: List[str] = []
    if direction == "up":
        out.append(f"ip route replace default dev px-{e.code} table {p} 2>/dev/null || true")
        out.append(f"ip route replace blackhole default metric {BLACKHOLE_METRIC} table {p}")
        out.append(f"while ip rule del fwmark {m} lookup {p} priority {EXIT_MARK_PRIO} 2>/dev/null; do :; done; "
                   f"ip rule add fwmark {m} lookup {p} priority {EXIT_MARK_PRIO}")
        for f in cfg.families():
            out.append(f"{f} route replace {cfg.family_net(f)} dev {cfg.WG_IF} table {w}")
            out.append(f"{f} route replace default dev wx-{e.code} table {w} 2>/dev/null || true")
            out.append(f"{f} route replace blackhole default metric {BLACKHOLE_METRIC} table {w}")
        for c in snap.exit_clients(e.code):
            out += exit_client_cmds(cfg, snap, c, "up")
    else:
        for c in snap.exit_clients(e.code):
            out += exit_client_cmds(cfg, snap, c, "down")
        for f in reversed(cfg.families()):
            out.append(f"{f} route flush table {w} 2>/dev/null || true")
        out.append(f"while ip rule del fwmark {m} lookup {p} priority {EXIT_MARK_PRIO} 2>/dev/null; do :; done")
        out.append(f"ip route flush table {p} 2>/dev/null || true")
    return out


def hook_cmds(cfg: Config, snap: Snapshot, pub_if: str, direction: str) -> List[str]:
    """wg0's PostUp (up) or PostDown (down). RT_TABLE is filled either way; only
    the policy rule depends on WARP. warp's route is optional here (warp may be
    down; its own PostUp adds it)."""
    rt, wg, warp = cfg.RT_TABLE, cfg.WG_IF, cfg.WARP_IF
    out: List[str] = []
    if direction == "up":
        for f in cfg.families():
            out.append(f"{f} route replace {cfg.family_net(f)} dev {wg} table {rt}")
            out.append(f"{f} route replace default dev {warp} table {rt} 2>/dev/null || true")
            out.append(f"{f} route replace blackhole default metric {BLACKHOLE_METRIC} table {rt}")
        if cfg.WARP == "on":
            out += warp_rule_cmds(cfg, True)
        out += ipt_cmds(ipt_rules(cfg, pub_if, snap.exits), "up")
        for e in snap.exits:
            out += exit_hook_cmds(cfg, snap, e, "up")
    else:
        for e in reversed(snap.exits):
            out += exit_hook_cmds(cfg, snap, e, "down")
        out += ipt_cmds(reversed(ipt_rules(cfg, pub_if, snap.exits)), "down")
        out += list(reversed(warp_rule_cmds(cfg, False)))
        for f in reversed(cfg.families()):
            out.append(f"{f} route del blackhole default metric {BLACKHOLE_METRIC} table {rt} 2>/dev/null || true")
            out.append(f"{f} route del default dev {warp} table {rt} 2>/dev/null || true")
            out.append(f"{f} route del {cfg.family_net(f)} dev {wg} table {rt} 2>/dev/null || true")
    return out
