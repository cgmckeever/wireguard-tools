"""Facts about this machine (read-only): IPv6, routes, hostname, public interface."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from typing import Optional

from . import store
from .system import Runner


@dataclass
class Facts:
    kernel_ipv6: bool
    has_ipv6: bool          # an IPv6 default route
    hostname: str
    routes4: str            # `ip -4 route show table all`, for picking a free subnet
    default_route: str      # the main table's first IPv4 default route

    def accept_ra(self, runner: Runner, ifc: str) -> Optional[str]:
        out = runner.query(["sysctl", "-n", f"net.ipv6.conf.{ifc}.accept_ra"]).strip()
        return out or None


def gather(runner: Runner) -> Facts:
    disabled = runner.query(["sysctl", "-n", "net.ipv6.conf.all.disable_ipv6"]).strip()
    return Facts(
        kernel_ipv6=os.path.exists("/proc/net/if_inet6") and disabled == "0",
        has_ipv6=bool(runner.query(["ip", "-6", "route", "show", "default"]).strip()),
        hostname=socket.gethostname(),
        routes4=runner.query(["ip", "-4", "route", "show", "table", "all"]),
        default_route=(runner.query(["ip", "-4", "route", "show", "default"]).splitlines() or [""])[0],
    )


def route_key(route: str) -> str:
    """"via X dev Y" from a route line, ignoring volatile fields like metric/proto."""
    f = route.split()
    via = dev = ""
    for i, w in enumerate(f[:-1]):
        if w == "via":
            via = f[i + 1]
        if w == "dev":
            dev = f[i + 1]
    return f"via {via} dev {dev}"


def route_dev(route: str) -> str:
    return route_key(route).split()[3] if len(route_key(route).split()) > 3 else ""


def pub_if(state_text: Optional[str], facts: Facts) -> str:
    """Public interface: recorded at install, else the current default route's."""
    return store.state_get(state_text, "PUB_IF") or route_dev(facts.default_route)


def resolve4(host: str) -> Optional[str]:
    """First IPv4 address of <host> (None if it doesn't resolve)."""
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET)
    except OSError:
        return None
    return infos[0][4][0] if infos else None
