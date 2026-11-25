"""Address maths: IPv4 integers, the clients' IPv6 /64, random subnets."""

from __future__ import annotations

import ipaddress
import os
import random
import re
from typing import Iterator, List, Optional, Tuple, Union

from . import host
from .errors import WgtError

SUBNET_OCTETS = range(200, 256)   # picked subnets: high in 10.x, away from home and office LANs


def ip2int(ip: str) -> int:
    m = re.fullmatch(r"([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})", ip)
    if not m or any(int(x) > 255 for x in m.groups()):
        raise WgtError(f"not an IPv4 address: '{ip}'")
    a, b, c, d = (int(x) for x in m.groups())
    return (a << 24) | (b << 16) | (c << 8) | d


def network_int(cidr: str) -> int:
    """A subnet's network address as an integer: 10.8.0.0/24 -> 10.8.0.0's."""
    base, pfx = cidr.split("/")
    return ip2int(base) & ((0xFFFFFFFF << (32 - int(pfx))) & 0xFFFFFFFF)


def int2ip(n: int) -> str:
    return f"{(n >> 24) & 255}.{(n >> 16) & 255}.{(n >> 8) & 255}.{n & 255}"


_SUBNET6 = re.compile(r"([0-9a-f]{1,4}:){0,3}[0-9a-f]{1,4}::/64")


def canon_subnet6(value: str) -> Optional[str]:
    """A "<groups>::/64" prefix as ip(8) and iptables print it (lowercase, no
    leading or trailing zeros); None if it isn't one."""
    p = value.lower()
    if not _SUBNET6.fullmatch(p):
        return None
    canon = str(ipaddress.IPv6Network(p))
    return None if canon == "::/64" else canon


def random_subnet6() -> str:
    """A new random ULA /64 (RFC 4193): fdXX:XXXX:XXXX::/64."""
    h = os.urandom(5).hex()
    return str(canon_subnet6(f"fd{h[0:2]}:{h[2:6]}:{h[6:10]}::/64"))


def _route_nets(routes: str) -> Iterator[Tuple[str, Union[ipaddress.IPv4Network, ipaddress.IPv6Network]]]:
    """Each route line in <routes> that starts with a network, and that network (the
    default route and a "blackhole x" line don't)."""
    for line in routes.splitlines():
        fields = line.split()
        if not fields:
            continue
        try:
            yield line, ipaddress.ip_network(fields[0], strict=False)
        except ValueError:
            continue


def routed_overlap(subnet: str, routes: str, own_if: str) -> str:
    """The first network in <routes> (`ip -4 route`) overlapping <subnet>, not
    counting <own_if>'s; "" if none. The default route isn't one."""
    want = ipaddress.ip_network(subnet)
    for line, routed in _route_nets(routes):
        # The route's own interface, not a substring: wg0's would also skip a wg0b's routes.
        if host.route_dev(line) != own_if and want.overlaps(routed):
            return line.split()[0]
    return ""


def _routed(net: str, routes: str) -> bool:
    """A route in <routes> already points into 10.a.b (<net>), or covers it (10.0.0.0/8:
    setup's refuse_routed_subnet would refuse the pick, as routed_overlap sees it)."""
    if re.search(r"(^|[^0-9.])" + re.escape(net) + r"\.", routes, re.M):
        return True
    want = ipaddress.ip_network(f"{net}.0/24")
    return any(want.overlaps(routed) for _, routed in _route_nets(routes))


def next_free_subnet(routes: str, used: List[str]) -> str:
    """The first 10.<200-255>.<200-255>.0/24, in order, that no route points
    into and that overlaps none of <used> (the other servers' subnets, from
    wgt-ctl init); a random one if all are taken."""
    taken = []
    for u in used:
        try:
            taken.append(ipaddress.ip_network(u.strip(), strict=False))
        except ValueError:
            continue
    for a in SUBNET_OCTETS:
        for b in SUBNET_OCTETS:
            cand = ipaddress.ip_network(f"10.{a}.{b}.0/24")
            if not _routed(f"10.{a}.{b}", routes) and not any(cand.overlaps(t) for t in taken):
                return str(cand)
    return random_subnet(routes)


def random_subnet(routes: str, rng: Optional[random.Random] = None) -> str:
    """A random 10.<200-255>.<200-255>.0/24 that no route in <routes> (the output
    of `ip -4 route show table all`) already points into."""
    rng = rng or random.Random()
    net = ""
    for _ in range(20):
        net = f"10.{rng.choice(SUBNET_OCTETS)}.{rng.choice(SUBNET_OCTETS)}"
        if not _routed(net, routes):
            break
    return f"{net}.0/24"
