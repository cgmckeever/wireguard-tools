"""Config files, rendered as text: wg0.conf, warp.conf (and each exit city's
wx-<code>.conf), px-<code>.conf, client configs and the templates. Output is
byte-identical to the bash version, so switching tools rewrites nothing."""

from __future__ import annotations

import os
import re
from typing import Callable, List, Optional, Tuple

from . import rules
from .config import Config
from .const import EXIT_PROTON_MTU, MARKER, TEMPLATE_DIR
from .errors import WgtError
from .store import Exit, Snapshot, client_exit

_KEY = re.compile(r"\s*=.*")
_VAL = re.compile(r"^[^=]*=\s*")
_COMMENT_OR_BLANK = re.compile(r"^\s*(#|$)")


def render_template(name: str, template_dir: str = TEMPLATE_DIR, **values: str) -> str:
    """templates/<name> with each {{KEY}} replaced; an unfilled {{KEY}} is an
    error. Ends with exactly one newline (as `printf '%s\\n' "$(<file)"`)."""
    path = os.path.join(template_dir, name)
    try:
        with open(path, encoding="utf-8") as f:
            out = f.read().rstrip("\n")
    except FileNotFoundError:
        raise WgtError(f"missing template: {path}")
    for k, v in values.items():
        out = out.replace("{{" + k + "}}", v)
    m = re.search(r"\{\{[A-Z_]+\}\}", out)
    if m:
        raise WgtError(f"template {name}: no value for {m.group(0)}")
    return out.rstrip("\n") + "\n"


def _kv(line: str) -> Tuple[str, str]:
    """(key, value) of a "Key = value" line, as the bash version's awk split it."""
    k = _KEY.sub("", line, count=1).lstrip()
    v = _VAL.sub("", line, count=1).rstrip()
    return k, v


# --- wg0.conf ---

def render_wg_conf(cfg: Config, snap: Snapshot, pub_if: str, psk: Callable[[str], str]) -> str:
    """wg0.conf: interface, hook rules, and one [Peer] per enabled client (a
    disabled one leaves just a comment)."""
    p = cfg.paths
    addr = f"{cfg.server_ip}/{cfg.pfx}" + (f", {cfg.p6}::1/64" if cfg.ipv6 else "")
    out: List[str] = [
        f"{MARKER} (WARP {cfg.WARP}); peers are rebuilt from {p.client_db} (use wg-client.sh)",
        "[Interface]",
        f"Address = {addr}",
        f"ListenPort = {cfg.WG_PORT}",
        f"MTU = {cfg.WG_MTU}",
        f"PostUp = wg set %i private-key {p.server_key}",
    ]
    out += [f"PostUp = {line}" for line in rules.hook_cmds(cfg, snap, pub_if, "up")]
    out += [f"PostDown = {line}" for line in rules.hook_cmds(cfg, snap, pub_if, "down")]
    text = "\n".join(out) + "\n"
    for c in snap.clients:
        if snap.is_disabled(c.name):
            text += f"\n# {c.name} ({c.ip}): disabled (wg-client.sh enable {c.name})\n"
            continue
        text += f"\n[Peer]\n# {c.name}\nPublicKey = {c.pub}\n"
        key = psk(c.name)
        if key:
            text += f"PresharedKey = {key}\n"
        text += f"AllowedIPs = {cfg.client_addrs(c.ip).replace(',', ', ', 1)}\n"
    return text


# --- warp.conf and exit cities' wx-<code>.conf ---

def profile_endpoint(profile: str) -> str:
    for line in profile.splitlines():
        if re.match(r"^\s*Endpoint\s*=", line):
            return _VAL.sub("", line, count=1)
    return ""


def render_warp_conf(cfg: Config, profile: str, has_ipv6: bool,
                     resolve4: Callable[[str], Optional[str]],
                     table: Optional[int] = None, mark: Optional[int] = None,
                     warn: Callable[[str], None] = lambda m: None) -> str:
    """A WARP tunnel config from a wgcf profile: Table = off, our MTU, no DNS,
    WARP's IPv6 address only when client IPv6 goes through it, keepalive,
    loose rp_filter (strict would drop WARP's replies) and the routing table's
    default route(s), which go when the tunnel goes down.

    For an exit city's WARP give <table> and <mark>: its routes go to <table>,
    its packets carry <mark> (routed through Proton), and its endpoint is IPv4
    (Proton carries only IPv4 for it). Otherwise the endpoint is made IPv4 only
    on hosts without IPv6."""
    ep = profile_endpoint(profile)
    if (mark is not None or not has_ipv6) and ep:
        host, _, port = ep.rpartition(":")
        if not re.match(r"^[0-9.]+$", host):
            ip4 = resolve4(host)
            if ip4:
                ep = f"{ip4}:{port}"
            else:
                warn(f"could not resolve {host} to IPv4; keeping it")
    if table is not None:
        routes = [f"{f} route replace default dev %i table {table}" for f in cfg.families()]
    else:
        routes = rules.warp_route(cfg)

    def addrs(key: str, value: str) -> Optional[str]:
        parts = [a for a in re.split(r"\s*,\s*", value) if a and (cfg.ipv6 or ":" not in a)]
        return f"{key} = {', '.join(parts)}" if parts else None

    out = [f"{MARKER} from wgcf-profile.conf; other routing/NAT is in wg0.conf"]
    sec = ""
    for line in profile.splitlines():
        if re.match(r"^\s*\[Interface\]", line):
            out += ["[Interface]", f"MTU = {cfg.WARP_MTU}", "Table = off"]
            if mark is not None:
                out.append(f"FwMark = {mark}")
            out.append("PostUp = sysctl -q -w net.ipv4.conf.%i.rp_filter=2")
            out += [f"PostUp = {r}" for r in routes]
            sec = "i"
            continue
        if re.match(r"^\s*\[Peer\]", line):
            out += ["", "[Peer]"]
            sec = "p"
            continue
        if _COMMENT_OR_BLANK.match(line):
            continue
        k, v = _kv(line)
        if sec == "i" and (k in ("DNS", "MTU", "Table", "FwMark") or re.match(r"^(Pre|Post)(Up|Down)$", k)):
            continue
        if k in ("Address", "AllowedIPs"):
            a = addrs(k, v)
            if a:
                out.append(a)
            continue
        if k == "Endpoint":
            out.append(f"Endpoint = {ep}")
            continue
        if k == "PersistentKeepalive":
            continue
        out.append(f"{k} = {v}")
    out.append("PersistentKeepalive = 25")
    return "\n".join(out) + "\n"


# --- exit cities' px-<code>.conf ---

def render_proton_conf(raw: str, e: Exit) -> str:
    """px-<code>.conf from an imported Proton config: its key, IPv4 address and
    peer only; Table = off, our MTU, no DNS, loose rp_filter, and the exit's
    Proton table's default route (the blackhole stays when the tunnel goes)."""
    out = [f"{MARKER} exit {e.code}: Proton tunnel from exits/{e.code}.proton.conf; WARP inside it is wx-{e.code}"]
    sec = ""
    for line in raw.splitlines():
        if re.match(r"^\s*\[Interface\]", line):
            out += ["[Interface]", f"MTU = {EXIT_PROTON_MTU}", "Table = off",
                    "PostUp = sysctl -q -w net.ipv4.conf.%i.rp_filter=2",
                    f"PostUp = ip route replace default dev %i table {rules.exit_ptable(e)}"]
            sec = "i"
            continue
        if re.match(r"^\s*\[Peer\]", line):
            out += ["", "[Peer]"]
            sec = "p"
            continue
        if _COMMENT_OR_BLANK.match(line):
            continue
        k, v = _kv(line)
        if sec == "i" and k == "PrivateKey":
            out.append(f"{k} = {v}")
        elif sec == "i" and k == "Address":
            v4 = next((a for a in re.split(r"\s*,\s*", v) if "." in a), None)
            if v4:
                out.append(f"Address = {v4}")
        elif sec == "p" and k in ("PublicKey", "PresharedKey", "Endpoint"):
            out.append(f"{k} = {v}")
    out += ["AllowedIPs = 0.0.0.0/0", "PersistentKeepalive = 25"]
    return "\n".join(out) + "\n"


def proton_hint(raw: str) -> str:
    """The comment Proton puts under [Peer], e.g. "FR#252" ("" if none)."""
    in_peer = False
    for line in raw.splitlines():
        if re.match(r"^\s*\[Peer\]", line):
            in_peer = True
        elif in_peer and re.match(r"^\s*#", line):
            return re.sub(r"^\s*#\s*", "", line)
    return ""


# --- client configs ---

def exit_desc(snap: Snapshot, name: str) -> str:
    """The "# exit:" line of a client's config."""
    code = client_exit(name)
    if code:
        e = snap.exit(code)
        return f"{code} = {e.label if e else ''} (Proton -> Cloudflare WARP)"
    return "default (this server's WARP setting: wg-setup.sh warp on|off)"


def render_client_conf(cfg: Config, snap: Snapshot, name: str, ip: str, private_key: str,
                       server_pub: str, psk: str, endpoint: str, allowed: str) -> str:
    return render_template(
        "client.conf.tmpl",
        NAME=name, SERVER_NAME=cfg.SERVER_NAME, EXIT=exit_desc(snap, name), PRIVATE_KEY=private_key,
        ADDRESS=cfg.client_addrs(ip).replace(",", ", ", 1), DNS=cfg.WG_DNS, MTU=cfg.WG_MTU,
        SERVER_PUBLIC_KEY=server_pub, PRESHARED_KEY=psk, ENDPOINT=f"{endpoint}:{cfg.WG_PORT}",
        ALLOWED_IPS=allowed.replace(",", ", "),
    )


# --- sysctl and the systemd drop-in ---

def sysctl_lines(cfg: Config, pub_if: str, has_ipv6: bool, accept_ra: Optional[str]) -> List[str]:
    """The forwarding sysctls. With IPv6 forwarding on, a host that gets its own
    IPv6 from router advertisements would stop accepting them (and lose its
    IPv6 route) unless accept_ra is 2."""
    out = ["net.ipv4.ip_forward = 1"]
    if not cfg.ipv6:
        return out
    out.append("net.ipv6.conf.all.forwarding = 1")
    if has_ipv6 and accept_ra != "0":   # unreadable counts as "accepts", as before
        out.append(f"net.ipv6.conf.{pub_if}.accept_ra = 2")
    return out


def render_sysctl(lines: List[str]) -> str:
    return "\n".join([MARKER] + lines) + "\n"


def render_dropin(cfg: Config) -> str:
    return render_template("wg-quick-after-warp.conf.tmpl", WG_IF=cfg.WG_IF, WARP_IF=cfg.WARP_IF)
