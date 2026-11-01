"""Address maths: IPv4 integers, the clients' IPv6 /64, random subnets."""

from __future__ import annotations

import os
import random
import re


def ip2int(ip: str) -> int:
    a, b, c, d = (int(x) for x in ip.split("."))
    return (a << 24) | (b << 16) | (c << 8) | d


def int2ip(n: int) -> str:
    return f"{(n >> 24) & 255}.{(n >> 16) & 255}.{(n >> 8) & 255}.{n & 255}"


_SUBNET6 = re.compile(r"^([0-9a-f]{1,4}:){0,3}[0-9a-f]{1,4}::/64$")


def canon_subnet6(value: str) -> str | None:
    """Lowercase a "<groups>::/64" prefix and drop leading zeros, as ip(8)
    prints it; None if it isn't one."""
    p = value.lower()
    if not _SUBNET6.match(p):
        return None
    groups = p[: -len("::/64")].split(":")
    return ":".join(format(int(g, 16), "x") for g in groups) + "::/64"


def random_subnet6() -> str:
    """A new random ULA /64 (RFC 4193): fdXX:XXXX:XXXX::/64."""
    h = os.urandom(5).hex()
    return canon_subnet6(f"fd{h[0:2]}:{h[2:6]}:{h[6:10]}::/64") or ""


def random_subnet(routes: str, rng: random.Random | None = None) -> str:
    """A random 10.<1-10>.<1-10>.0/24 that no route in <routes> (the output of
    `ip -4 route show table all`) already points into."""
    rng = rng or random.Random()
    net = ""
    for _ in range(20):
        net = f"10.{rng.randrange(10) + 1}.{rng.randrange(10) + 1}"
        if not re.search(r"(^|[^0-9.])" + re.escape(net) + r"\.", routes, re.M):
            break
    return f"{net}.0/24"
