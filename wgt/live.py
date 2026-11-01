"""Read-only questions about what's running: interfaces, handshakes,
rules, routes, units, Cloudflare's trace. All through Runner.query/ok, so
tests can answer them."""

from __future__ import annotations

import os
import re
import time
from typing import Dict, List, Optional, Tuple

from .const import TRACE_URL
from .system import Runner


def if_exists(r: Runner, ifc: str) -> bool:
    return r.ok(["ip", "link", "show", "dev", ifc])


def handshakes(r: Runner, ifc: str) -> Dict[str, int]:
    """public key -> unix time of its latest handshake (0 = never)."""
    out: Dict[str, int] = {}
    for line in r.query(["wg", "show", ifc, "latest-handshakes"]).splitlines():
        f = line.split()
        if len(f) >= 2 and f[1].isdigit():
            out[f[0]] = int(f[1])
    return out


def transfers(r: Runner, ifc: str) -> Dict[str, Tuple[int, int]]:
    """public key -> (rx bytes, tx bytes)."""
    out: Dict[str, Tuple[int, int]] = {}
    for line in r.query(["wg", "show", ifc, "transfer"]).splitlines():
        f = line.split()
        if len(f) >= 3:
            out[f[0]] = (int(f[1]), int(f[2]))
    return out


def newest_hs(r: Runner, ifc: str) -> int:
    """Unix time of an interface's newest handshake (0 if none)."""
    return max(handshakes(r, ifc).values() or [0])


def hs_recent(r: Runner, ifc: str, within: int, now: Optional[float] = None) -> bool:
    newest = newest_hs(r, ifc)
    return newest > 0 and (now or time.time()) - newest <= within


def listen_port(r: Runner, ifc: str) -> str:
    return r.query(["wg", "show", ifc, "listen-port"]).strip()


def rules(r: Runner, family: str = "ip") -> str:
    return r.query(family.split() + ["rule", "show"])


def table(r: Runner, family: str, table: object) -> str:
    return r.query(family.split() + ["route", "show", "table", str(table)])


def sysctl(r: Runner, key: str) -> str:
    return r.query(["sysctl", "-n", key]).strip()


def unit_enabled(r: Runner, unit: str) -> bool:
    return r.ok(["systemctl", "is-enabled", "-q", unit])


def unit_active(r: Runner, unit: str) -> bool:
    return r.ok(["systemctl", "is-active", "-q", unit])


def unit_state(r: Runner, unit: str) -> Tuple[str, str]:
    """(is-active, is-enabled) as systemctl prints them."""
    return (r.query(["systemctl", "is-active", unit]).strip(),
            r.query(["systemctl", "is-enabled", unit]).strip())


def unit_prop(r: Runner, unit: str, prop: str) -> str:
    return r.query(["systemctl", "show", "-p", prop, "--value", unit]).strip()


def trace(r: Runner, *curl_args: str, url: str = TRACE_URL, tries: int = 3) -> str:
    """Cloudflare's trace (key=value lines), up to <tries> tries."""
    for i in range(tries):
        out = r.query(["curl", "-fsS", "--max-time", "10", *curl_args, url])
        if out:
            return out
        if i < tries - 1:
            time.sleep(2)
    return ""


def trace_field(text: str, field: str) -> str:
    for line in text.splitlines():
        if line.startswith(field + "="):
            return line[len(field) + 1:]
    return ""


def trace_summary(text: str) -> str:
    """"ip=... colo=... loc=... warp=..." from a trace."""
    keep = [line for line in text.splitlines() if line.split("=", 1)[0] in ("ip", "colo", "loc", "warp")]
    return " ".join(keep)


def is_warp_trace(text: str) -> bool:
    """True if a trace (lines, or a one-line summary) shows WARP."""
    t = " " + text.replace("\n", " ") + " "
    return " warp=on " in t or " warp=plus " in t


def ipt_count(r: Runner, cmd: str, tbl: str, chain: str, rule: str) -> int:
    """How many copies of a firewall rule there are (via -S, which prints
    "-p tcp -m tcp" for "-p tcp")."""
    pat = ""
    for part in re.split(r"(-p (?:tcp|udp) )", f"-A {chain} {rule}"):
        m = re.fullmatch(r"-p (tcp|udp) ", part)
        pat += f"-p {m.group(1)} (-m {m.group(1)} )?" if m else re.escape(part)
    rx = re.compile("^" + pat + "$")
    return sum(1 for line in r.query([cmd, "-t", tbl, "-S", chain]).splitlines() if rx.match(line))


def ipv6_global_addr(r: Runner, ifc: str) -> str:
    for line in r.query(["ip", "-6", "addr", "show", "dev", ifc, "scope", "global"]).splitlines():
        f = line.split()
        if f and f[0] == "inet6":
            return f[1].split("/")[0]
    return ""


def addr_of(r: Runner, ifc: str, family: str = "-4") -> str:
    """The first address/prefix on an interface (global scope for IPv6)."""
    args = ["ip", family, "-o", "addr", "show", "dev", ifc] + (["scope", "global"] if family == "-6" else [])
    for line in r.query(args).splitlines():
        f = line.split()
        if len(f) >= 4:
            return f[3]
    return ""


def mtu(ifc: str) -> str:
    try:
        with open(f"/sys/class/net/{ifc}/mtu") as f:
            return f.read().strip()
    except OSError:
        return ""


def proc_exists(path: str) -> bool:
    return os.path.exists(path)


def lines(text: str) -> List[str]:
    return text.splitlines()
