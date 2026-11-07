"""Facts about this machine (read-only): IPv6, routes, hostname, public interface."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from typing import Optional

from . import store
from .errors import WgtError
from .system import Runner


@dataclass
class Facts:
    """What setup and the checks need to know about this machine."""

    kernel_ipv6: bool
    has_ipv6: bool          # an IPv6 default route
    hostname: str
    routes4: str            # `ip -4 route show table all`, for picking a free subnet
    default_route: str      # the main table's first IPv4 default route


def gather(runner: Runner) -> Facts:
    """Read this machine's facts (read-only commands)."""
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
    """The interface of a route line (also for one without "via", e.g. "default dev venet0")."""
    f = route.split()
    return next((f[i + 1] for i in range(len(f) - 1) if f[i] == "dev"), "")


def pub_if(state_text: Optional[str], facts: Facts) -> str:
    """Public interface: recorded at install, else the current default route's."""
    return store.state_get(state_text, "PUB_IF") or route_dev(facts.default_route)


def resolve4(host: str) -> Optional[str]:
    """First IPv4 address of <host> (None if it doesn't resolve)."""
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET)
    except (OSError, UnicodeError):
        return None
    return str(infos[0][4][0]) if infos else None


def preflight(runner: Runner) -> None:
    """Root, OS and architecture checks; refuse anything unsupported."""
    if os.geteuid() != 0:
        raise WgtError("must run as root")
    try:
        with open("/etc/os-release") as f:
            info = dict(line.split("=", 1) for line in f.read().splitlines() if "=" in line)
    except OSError:
        raise WgtError("cannot read /etc/os-release; unsupported OS") from None
    osid = info.get("ID", "").strip('"')
    ver = info.get("VERSION_ID", "").strip('"')
    major = ver.split(".")[0]
    if not store.is_uint(major):
        raise WgtError(f"unsupported OS: {osid} {ver or '<no version>'} (need Debian 11+ or Ubuntu 20.04+)")
    if osid == "debian" and int(major) < 11:
        raise WgtError(f"unsupported Debian {ver} (need 11+)")
    if osid == "ubuntu" and int(major) < 20:
        raise WgtError(f"unsupported Ubuntu {ver} (need 20.04+)")
    if osid not in ("debian", "ubuntu"):
        raise WgtError(f"unsupported OS: {osid} {ver} (need Debian 11+ or Ubuntu 20.04+)")
    arch = runner.query(["dpkg", "--print-architecture"]).strip()
    if arch not in ("amd64", "arm64"):
        raise WgtError(f"unsupported architecture: {arch} (need amd64 or arm64)")
