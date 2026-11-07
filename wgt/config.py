"""Settings: where each value comes from (flag > environment > saved > default)
and the checks and derived values (subnet maths, IPv6, file paths)."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional

from . import net, store
from .const import (
    DEFAULTS,
    DEVICE_NET,
    EXIT_MAX,
    EXIT_PTABLE_BASE,
    EXIT_TABLE_BASE,
    SETTING_KEYS,
    Paths,
)
from .errors import WgtError

FLEET_SUBNETS_ENV = "WGT_FLEET_SUBNETS"   # set by wgt-ctl init: the other servers' subnets, comma-separated

_IFNAME = re.compile(r"[A-Za-z0-9_-]{1,15}")
_SERVER_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}")
_SUBNET4 = re.compile(r"([0-9]{1,3}\.){3}[0-9]{1,3}/([0-9]{1,2})")


_DNS = re.compile(r"[A-Za-z0-9.:-]+( *, *[A-Za-z0-9.:-]+)*")
_ENDPOINT = re.compile(r"\[[0-9a-fA-F:.]+\]|[0-9a-fA-F:]+|[A-Za-z0-9.-]+")


def valid_allowed_ips(value: str) -> bool:
    """A comma list of CIDRs, as a config's AllowedIPs."""
    try:
        for cidr in value.split(","):
            ipaddress.ip_network(cidr.strip(), strict=False)
    except ValueError:
        return False
    return True


def _in_range(value: str, lo: int, hi: int) -> bool:
    """An integer of ASCII digits in [lo, hi]."""
    return store.is_uint(value) and lo <= int(value) <= hi


def short_hostname_name(hostname: str) -> str:
    """The default server name: the short hostname, with anything outside
    [A-Za-z0-9_-] turned into '-', at most 32 characters; "wg" if empty."""
    short = hostname.split(".")[0]
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", short)[:32]
    return cleaned or "wg"


@dataclass
class Config:
    """The settings one run uses, plus where each came from."""

    values: Dict[str, str]
    sources: Dict[str, str] = field(default_factory=dict)   # key -> flag|env|saved|default|random
    saved: Dict[str, str] = field(default_factory=dict)      # the state file's values
    subnet_defaulted: bool = False                           # neither --subnet nor a saved one
    paths: Paths = field(default_factory=Paths)
    # Derived by finalize():
    pfx: int = 0
    net_int: int = 0
    server_ip: str = ""
    first_host: int = 0
    last_host: int = 0
    ipv6: bool = False
    p6: str = ""

    def __getattr__(self, name: str) -> str:
        """Each setting reads as an attribute: cfg.WG_IF is values["WG_IF"].
        The keys are SETTING_KEYS in const.py."""
        values = self.__dict__.get("values", {})
        if name in values:
            return values[name]
        raise AttributeError(name)

    @property
    def warp_conf(self) -> str:
        return self.paths.wg(f"{self.WARP_IF}.conf")

    @property
    def wg_conf(self) -> str:
        return self.paths.wg(f"{self.WG_IF}.conf")

    @property
    def dropin_dir(self) -> str:
        return self.paths.unit_file(f"wg-quick@{self.WG_IF}.service.d")

    @property
    def dropin(self) -> str:
        return f"{self.dropin_dir}/10-after-{self.WARP_IF}.conf"

    def finalize(self, kernel_ipv6: bool) -> None:
        """Check every value and derive the subnet addresses; raises WgtError."""
        v = self.values
        # These three go into the configs sent to devices.
        if not _DNS.fullmatch(v["WG_DNS"]):
            raise WgtError(f"bad DNS (addresses or search domains, comma-separated): {v['WG_DNS']!r}")
        if not valid_allowed_ips(v["CLIENT_ALLOWED_IPS"]):
            raise WgtError(f"bad allowed IPs (comma-separated CIDRs): {v['CLIENT_ALLOWED_IPS']!r}")
        if v["ENDPOINT"] and not _ENDPOINT.fullmatch(v["ENDPOINT"]):
            raise WgtError(f"bad endpoint (an address or hostname, no port; the port is --port): {v['ENDPOINT']!r}")
        if not _SERVER_NAME.fullmatch(v["SERVER_NAME"]):
            raise WgtError(f"server name must be 1-32 of [A-Za-z0-9_-]: '{v['SERVER_NAME']}'")
        for k in ("WG_IF", "WARP_IF"):
            if not _IFNAME.fullmatch(v[k]):
                raise WgtError(f"bad interface name: {v[k]}")
        if v["WG_IF"] == v["WARP_IF"]:
            raise WgtError("WG_IF and WARP_IF must differ")
        if not _in_range(v["WG_PORT"], 1, 65535):
            raise WgtError(f"bad port: {v['WG_PORT']}")
        if not _in_range(v["WG_MTU"], 1280, 9000):
            raise WgtError(f"bad MTU (1280-9000): {v['WG_MTU']}")
        if not _in_range(v["WARP_MTU"], 1280, 9000):
            raise WgtError(f"bad WARP MTU (1280-9000): {v['WARP_MTU']}")
        if int(v["WG_MTU"]) > int(v["WARP_MTU"]):
            raise WgtError(f"WG_MTU ({v['WG_MTU']}) must not exceed WARP_MTU ({v['WARP_MTU']})")
        if not _in_range(v["RT_TABLE"], 1, 252):
            raise WgtError(f"bad table (1-252): {v['RT_TABLE']}")
        table = int(v["RT_TABLE"])
        if EXIT_TABLE_BASE < table <= EXIT_PTABLE_BASE + EXIT_MAX:
            raise WgtError(f"table {table} is reserved for jumps "
                           f"({EXIT_TABLE_BASE + 1}-{EXIT_PTABLE_BASE + EXIT_MAX})")
        subnet = v["WG_SUBNET"]
        if not _SUBNET4.fullmatch(subnet):
            raise WgtError(f"bad subnet: {subnet}")
        base, pfx = subnet.split("/")
        if any(int(o) > 255 for o in base.split(".")):
            raise WgtError(f"bad subnet: {subnet}")
        if not _in_range(pfx, 16, 30):
            raise WgtError(f"subnet prefix must be /16 to /30: {subnet}")
        self.pfx = int(pfx)
        self.net_int = net.network_int(f"{base}/{self.pfx}")
        if net.int2ip(self.net_int) != base:
            raise WgtError(f"{subnet} is not a network address "
                           f"(did you mean {net.int2ip(self.net_int)}/{self.pfx}?)")
        if ipaddress.ip_network(subnet).overlaps(ipaddress.ip_network(DEVICE_NET)):
            raise WgtError(f"subnet {subnet} overlaps {DEVICE_NET}, the devices' static addresses")
        self.server_ip = net.int2ip(self.net_int + 1)
        self.first_host = self.net_int + 2
        self.last_host = self.net_int + (1 << (32 - self.pfx)) - 2

        # IPv6 is on when a /64 is set (setup picks one) and the kernel has
        # IPv6: WARP's IPv6 is a way out even on IPv4-only hosts.
        self.ipv6 = False
        self.p6 = ""
        sub6 = v.get("WG_SUBNET6", "")
        if sub6 not in ("", "off"):
            canon = net.canon_subnet6(sub6)
            if canon is None:
                raise WgtError(f"bad --subnet6 (want a /64 like fd12:3456:789a::/64, or off): {sub6}")
            v["WG_SUBNET6"] = canon
            self.p6 = canon[: -len("::/64")]
            self.ipv6 = kernel_ipv6

    # --- client addresses ---

    def ip6_of(self, ip: str) -> str:
        """The client IPv6 matching an IPv4 address: same host number in WG_SUBNET6."""
        host = net.ip2int(ip) - self.net_int
        if not 0 <= host <= self.last_host - self.net_int:
            raise WgtError(f"{ip} is not in {self.WG_SUBNET}")
        return f"{self.p6}::{format(host, 'x')}"

    def client_addrs(self, ip: str) -> str:
        """A client's addresses as "ip/32" or "ip/32,ip6/128" (comma list, no spaces)."""
        if self.ipv6:
            return f"{ip}/32,{self.ip6_of(ip)}/128"
        return f"{ip}/32"

    def families(self) -> List[str]:
        """"ip", plus "ip -6" when IPv6 is on."""
        return ["ip", "ip -6"] if self.ipv6 else ["ip"]

    def family_net(self, family: str) -> str:
        """The clients' subnet for "ip"/iptables (IPv4) or "ip -6"/ip6tables."""
        return self.WG_SUBNET6 if family in ("ip -6", "ip6tables") else self.WG_SUBNET


def resolve(flags: Mapping[str, str], env: Mapping[str, str], saved: Mapping[str, str],
            hostname: str = "wg", routes: str = "", paths: Optional[Paths] = None) -> Config:
    """Combine the sources: flag > environment > saved > default. An empty subnet becomes a random one, or with
    wgt-ctl init (which passes the other servers' subnets) the next free one."""
    values: Dict[str, str] = {}
    sources: Dict[str, str] = {}
    for k in SETTING_KEYS:
        if flags.get(k):
            values[k], sources[k] = flags[k], "flag"
        elif env.get(k):
            values[k], sources[k] = env[k], "env"
        elif saved.get(k):
            values[k], sources[k] = saved[k], "saved"
        else:
            values[k] = ""
    subnet_defaulted = not values["WG_SUBNET"]
    fleet = [u for u in env.get(FLEET_SUBNETS_ENV, "").split(",") if u.strip()]
    if subnet_defaulted and fleet:
        values["WG_SUBNET"] = net.next_free_subnet(routes, fleet)
        sources["WG_SUBNET"] = "next free in the fleet"
    elif subnet_defaulted:
        values["WG_SUBNET"] = net.random_subnet(routes)
        sources["WG_SUBNET"] = "random"
    for k, d in DEFAULTS.items():
        if not values[k]:
            values[k], sources[k] = d, "default"
    if not values["SERVER_NAME"]:
        values["SERVER_NAME"], sources["SERVER_NAME"] = short_hostname_name(hostname), "default"
    return Config(values=values, sources=sources, saved=dict(saved),
                  subnet_defaulted=subnet_defaulted, paths=paths or Paths())
