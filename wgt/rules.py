"""Routing, NAT and firewall rules, as shell lines: one source for wg0's
PostUp/PostDown, rollback, live changes and the tests.

How it fits together (see README "How it works"):
- WARP: RT_TABLE always holds "default dev warp" with a blackhole fallback;
  one policy rule per family, `from <subnet> lookup RT_TABLE`, sends the
  subnet there unless a config's own rule (below) says otherwise.
- Jump <code> with index n: client table 200+n (default dev wx-<code>,
  blackhole fallback) chosen by one rule per client address; jump table
  230+n (default dev px-<code>, blackhole) chosen by the fwmark of the jump's
  WARP tunnel. All of it lives in wg0's hooks, so a jump that is down blocks
  its clients instead of leaking them.
- A config without warp in its name skips our WARP: its rule picks the plain
  way out. A jump config's rule points at the jump table (px, NATed like any
  way out); a host config gets `lookup main`
  plus a blackhole rule behind it, so a missing main route (IPv6 on an
  IPv4-only server) stops there rather than falling through to WARP's rule.

Per client, then, the way out is exit (host | jump) x WARP (on | off):

             WARP on                       WARP off
  host       RT_TABLE (the WARP switch)    main (this server's IP)
  jump       client table 200+n (wx)       jump table 230+n (px)

Static device addresses (DEVICE_NET; see devices.py): wg0 holds the network's
first address, so the server's own connections to a device use it. The fixed
part is here: the nat chains and what leads to them, forwarding between devices
for that traffic only, and a rule making an address with no config in use unreachable rather
than a packet for a way out. The chains' rules are the device watchdog's.

Every line is idempotent, and "down" is the exact inverse of "up" in reverse
order. The rest of the text matches the bash version byte for byte (wg0.conf
embeds it)."""

from __future__ import annotations

from typing import Iterable, List, Sequence, Tuple

from .config import Config
from .const import (
    BLACKHOLE_METRIC,
    DEVICE_CHAIN,
    DEVICE_NET,
    DEVICE_RULE_PRIO,
    DEVICE_SOURCE_CHAIN,
    DIRECT_STOP_PRIO,
    EXIT_MARK_BASE,
    EXIT_MARK_PRIO,
    EXIT_PTABLE_BASE,
    EXIT_RULE_PRIO,
    EXIT_TABLE_BASE,
    RULE_PRIO,
)
from .store import Client, Exit, Snapshot

IptRule = Tuple[str, str, str, str]   # command, table, chain, rule


def exit_wtable(e: Exit) -> int:
    """The exit's client table: its clients' traffic, out through its WARP (wx)."""
    return EXIT_TABLE_BASE + e.idx


def exit_ptable(e: Exit) -> int:
    """The exit's jump table: its WARP tunnel's packets, out through the jump (px)."""
    return EXIT_PTABLE_BASE + e.idx


def exit_mark(e: Exit) -> int:
    """The fwmark on the exit's WARP packets, which picks its jump table."""
    return EXIT_MARK_BASE + e.idx


def out_ifs(cfg: Config, pub_if: str, exits: Sequence[Exit]) -> List[str]:
    """Every way out: WARP, the public interface, each jump's WARP tunnel
    and (for its WARP-off clients) its jump tunnel."""
    out = [cfg.WARP_IF, pub_if]
    for e in exits:
        out += exit_ifs(e)
    return out


def exit_ifs(e: Exit) -> List[str]:
    """A jump's ways out: its WARP tunnel, and its jump tunnel (WARP-off clients)."""
    return [e.wx, e.px]


def exit_ipt_rules(cfg: Config, e: Exit) -> List[IptRule]:
    """A jump's NAT/forwarding rules: through its WARP, and (IPv4) through the jump."""
    return ipt_rules_via(cfg, e.wx) + ipt_rules_via(cfg, e.px, v4_only=True)


def rule_spec(cfg: Config, family: str) -> str:
    return f"from {cfg.family_net(family)} lookup {cfg.RT_TABLE} priority {RULE_PRIO}"


def warp_route(cfg: Config) -> List[str]:
    """RT_TABLE's default route(s) out through warp (in wg0's PostUp and warp.conf's)."""
    return [f"{f} route replace default dev {cfg.WARP_IF} table {cfg.RT_TABLE}" for f in cfg.families()]


def ipt_rules_for(cfg: Config, cmd: str, out_if: str) -> List[IptRule]:
    """The NAT/forwarding rules for one family (iptables|ip6tables) and way out."""
    net = cfg.family_net(cmd)
    wg = cfg.WG_IF
    return [
        (cmd, "nat", "POSTROUTING", f"-s {net} -o {out_if} -j MASQUERADE"),
        (cmd, "filter", "FORWARD", f"-i {wg} -o {out_if} -j ACCEPT"),
        (cmd, "filter", "FORWARD", f"-i {out_if} -o {wg} -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"),
        (cmd, "mangle", "FORWARD", f"-o {out_if} -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu"),
    ]


def ipt_rules(cfg: Config, pub_if: str, exits: Sequence[Exit]) -> List[IptRule]:
    """Firewall rules for every way out (each matches only its own -o, so all
    can stay in place), plus the WireGuard port. Inserted (-I) so ufw/Docker
    DROP policies can't shadow them."""
    found: List[IptRule] = []
    v4_only = {e.px for e in exits}   # jump tunnels carry IPv4 only
    for cmd in ("iptables", "ip6tables"):
        if cmd == "ip6tables" and not cfg.ipv6:
            continue
        for out_if in out_ifs(cfg, pub_if, exits):
            if cmd == "iptables" or out_if not in v4_only:
                found += ipt_rules_for(cfg, cmd, out_if)
    found.append(("iptables", "filter", "INPUT", f"-p udp --dport {cfg.WG_PORT} -j ACCEPT"))
    return found + device_ipt_rules(cfg)


def device_ipt_rules(cfg: Config) -> List[IptRule]:
    """What leads to the device chains, and forwarding between devices for that
    traffic only (ctstate DNAT, both directions): any other device-to-device
    packet, by config address or over IPv6, is refused. The IPv4 refusal names
    what it spares, so it works wherever it lands in the chain. Written in
    `iptables -S` order: live.ipt_count compares the text."""
    wg = cfg.WG_IF
    found: List[IptRule] = [
        ("iptables", "nat", "PREROUTING", f"-d {DEVICE_NET} -i {wg} -j {DEVICE_CHAIN}"),
        ("iptables", "nat", "OUTPUT", f"-d {DEVICE_NET} -j {DEVICE_CHAIN}"),
        ("iptables", "nat", "POSTROUTING", f"-o {wg} -m conntrack --ctstate DNAT -j {DEVICE_SOURCE_CHAIN}"),
        ("iptables", "filter", "FORWARD", f"-i {wg} -o {wg} -m conntrack --ctstate DNAT -j ACCEPT"),
        ("iptables", "filter", "FORWARD",
         f"-i {wg} -o {wg} -m conntrack ! --ctstate DNAT -j REJECT --reject-with icmp-port-unreachable"),
    ]
    if cfg.ipv6:
        found.append(("ip6tables", "filter", "FORWARD",
                      f"-i {wg} -o {wg} -j REJECT --reject-with icmp6-port-unreachable"))
    return found


def device_rule(cfg: Config) -> str:
    return f"iif {cfg.WG_IF} to {DEVICE_NET} priority {DEVICE_RULE_PRIO} unreachable"


def ipt_rules_via(cfg: Config, out_if: str, v4_only: bool = False) -> List[IptRule]:
    """The NAT/forwarding rules for one way out (both families, unless <v4_only>)."""
    rules = ipt_rules_for(cfg, "iptables", out_if)
    if cfg.ipv6 and not v4_only:
        rules += ipt_rules_for(cfg, "ip6tables", out_if)
    return rules


def ipt_cmds(rules: Iterable[IptRule], up: bool) -> List[str]:
    """Firewall rules as idempotent shell lines that add (<up>) or remove them."""
    if up:
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


def client_table(snap: Snapshot, client: Client) -> str:
    """The routing table a config's own rules point at ("" for none: the
    subnet's WARP rule)."""
    off = snap.warp_off(client.name)
    if client.exit:
        e = snap.exit(client.exit)
        if e is None:
            return ""
        return str(exit_ptable(e) if off else exit_wtable(e))
    return "main" if off else ""


def client_rule_cmds(cfg: Config, snap: Snapshot, client: Client, up: bool) -> List[str]:
    """Policy rules sending one client's addresses (ip, ip6) to its way out
    (client_table): added (<up>) or removed. A WARP-off client without a
    jump also gets a blackhole rule right behind, so it can't reach WARP's rule.

    Adding never deletes first (the kernel refuses an identical rule), so
    re-applying live rules (setup, rotate) leaves no moment in which the client
    falls through to another way out; the blackhole goes in before `lookup main`."""
    table = client_table(snap, client)
    if not table or not client.ip:
        return []
    out = []
    for f in cfg.families():
        a = f"{cfg.ip6_of(client.ip)}/128" if f == "ip -6" else f"{client.ip}/32"
        lookup = f"from {a} lookup {table} priority {EXIT_RULE_PRIO}"
        stop = f"from {a} priority {DIRECT_STOP_PRIO} blackhole"
        if up:
            if table == "main":
                out.append(f"{f} rule add {stop} 2>/dev/null || true")
            out.append(f"{f} rule add {lookup} 2>/dev/null || true")
        else:
            out.append(f"while {f} rule del {lookup} 2>/dev/null; do :; done")
            if table == "main":
                out.append(f"while {f} rule del {stop} 2>/dev/null; do :; done")
    return out


def jump_table_cmds(cfg: Config, e: Exit) -> List[str]:
    """The jump table's routes (idempotent: safe to re-run live): out through
    the jump tunnel, a blackhole fallback, and for WARP-off clients, who use
    this table too, replies to other clients on wg0 and an IPv6 blackhole
    (jump tunnels carry IPv4 only)."""
    t = exit_ptable(e)
    out = [f"ip route replace default dev {e.px} table {t} 2>/dev/null || true",
           f"ip route replace blackhole default metric {BLACKHOLE_METRIC} table {t}"]
    for fam in cfg.families():
        out.append(f"{fam} route replace {cfg.family_net(fam)} dev {cfg.WG_IF} table {t}")
        if fam == "ip -6":
            out.append(f"{fam} route replace blackhole default metric {BLACKHOLE_METRIC} table {t}")
    return out


def exit_hook_cmds(cfg: Config, snap: Snapshot, e: Exit, up: bool) -> List[str]:
    """One exit's part of wg0's PostUp (<up>) or PostDown: its tables with blackhole fallbacks, the
    fwmark rule sending its WARP tunnel's packets through the jump, and its
    clients' rules (its firewall rules come with ipt_rules)."""
    client_table, jump_table, mark = exit_wtable(e), exit_ptable(e), exit_mark(e)
    out: List[str] = []
    if up:
        out += jump_table_cmds(cfg, e)
        out.append(f"while ip rule del fwmark {mark} lookup {jump_table} priority {EXIT_MARK_PRIO} 2>/dev/null; "
                   f"do :; done; ip rule add fwmark {mark} lookup {jump_table} priority {EXIT_MARK_PRIO}")
        for fam in cfg.families():
            out.append(f"{fam} route replace {cfg.family_net(fam)} dev {cfg.WG_IF} table {client_table}")
            out.append(f"{fam} route replace default dev {e.wx} table {client_table} 2>/dev/null || true")
            out.append(f"{fam} route replace blackhole default metric {BLACKHOLE_METRIC} table {client_table}")
        for c in snap.exit_clients(e.code):
            out += client_rule_cmds(cfg, snap, c, up=True)
    else:
        for c in snap.exit_clients(e.code):
            out += client_rule_cmds(cfg, snap, c, up=False)
        for fam in reversed(cfg.families()):
            out.append(f"{fam} route flush table {client_table} 2>/dev/null || true")
        out.append(f"while ip rule del fwmark {mark} lookup {jump_table} priority {EXIT_MARK_PRIO} 2>/dev/null; "
                   "do :; done")
        for fam in reversed(cfg.families()):
            out.append(f"{fam} route flush table {jump_table} 2>/dev/null || true")
    return out


def hook_cmds(cfg: Config, snap: Snapshot, pub_if: str, up: bool) -> List[str]:
    """wg0's PostUp (<up>) or PostDown. RT_TABLE is filled either way; only
    the policy rule depends on WARP. warp's route is optional here (warp may be
    down; its own PostUp adds it)."""
    rt, wg, warp = cfg.RT_TABLE, cfg.WG_IF, cfg.WARP_IF
    out: List[str] = []
    if up:
        for f in cfg.families():
            out.append(f"{f} route replace {cfg.family_net(f)} dev {wg} table {rt}")
            out.append(f"{f} route replace default dev {warp} table {rt} 2>/dev/null || true")
            out.append(f"{f} route replace blackhole default metric {BLACKHOLE_METRIC} table {rt}")
        if cfg.warp_on:
            out += warp_rule_cmds(cfg, True)
        out += [f"iptables -t nat -N {c} 2>/dev/null || true" for c in (DEVICE_CHAIN, DEVICE_SOURCE_CHAIN)]
        out += ipt_cmds(ipt_rules(cfg, pub_if, snap.exits), up=True)
        spec = device_rule(cfg)
        out.append(f"while ip rule del {spec} 2>/dev/null; do :; done; ip rule add {spec}")
        for e in snap.exits:
            out += exit_hook_cmds(cfg, snap, e, up=True)
        for c in snap.direct_clients():
            out += client_rule_cmds(cfg, snap, c, up=True)
    else:
        for c in reversed(snap.direct_clients()):
            out += client_rule_cmds(cfg, snap, c, up=False)
        for e in reversed(snap.exits):
            out += exit_hook_cmds(cfg, snap, e, up=False)
        out.append(f"while ip rule del {device_rule(cfg)} 2>/dev/null; do :; done")
        out += ipt_cmds(reversed(ipt_rules(cfg, pub_if, snap.exits)), up=False)
        for chain in (DEVICE_SOURCE_CHAIN, DEVICE_CHAIN):
            out.append(f"iptables -t nat -F {chain} 2>/dev/null || true")
            out.append(f"iptables -t nat -X {chain} 2>/dev/null || true")
        out += list(reversed(warp_rule_cmds(cfg, False)))
        for f in reversed(cfg.families()):
            out.append(f"{f} route del blackhole default metric {BLACKHOLE_METRIC} table {rt} 2>/dev/null || true")
            out.append(f"{f} route del default dev {warp} table {rt} 2>/dev/null || true")
            out.append(f"{f} route del {cfg.family_net(f)} dev {wg} table {rt} 2>/dev/null || true")
    return out
