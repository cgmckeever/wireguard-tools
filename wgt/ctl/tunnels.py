"""~/wg-tunnels ($WG_TUNNELS): where this machine keeps WireGuard configs, shared
with wireguard-tunnels. wgt-ctl uses two kinds of folder there, made on demand
(0700, like the root):

  servers/<server>/carriers/   jump configs staged for a server (jumpfiles.py)
  devices/<device>/config/     client configs as the servers issued them (below)

Everything else under it belongs to wireguard-tunnels.

Delivery: a server's client configs are copied to devices/<device>/config/.
The server's copy is the live one, so it wins over a local copy of the same
config. A local file is the same config when it has the same private key
(rename keeps keys), or else the same "# wireguard-tools client: <config> on
<server>" line. A matched file keeps its name; a new one is <server>.<config>.conf.
A file that would be overwritten but isn't a copy of that config is left alone.
A copy whose config the server no longer has becomes a .bak, as does the old
text of a copy that changes."""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .. import store
from ..errors import WgtError
from . import remote
from .remote import Server, out

HEADER = re.compile(r"(?m)^# wireguard-tools client: (\S+) on (\S+)[ \t]*$")
PRIVATE_KEY = re.compile(r"(?mi)^[ \t]*PrivateKey[ \t]*=[ \t]*(\S+)")
MARK = "\x1e"   # starts each file's name line in a fetch (never in a config)


def root() -> str:
    return os.path.expanduser(os.environ.get("WG_TUNNELS") or "~/wg-tunnels")


def carriers_dir(server: str) -> str:
    """Where <server>'s jump configs wait to be pushed."""
    return os.path.join(root(), "servers", server, "carriers")


def anchor(server: str) -> str:
    """servers/<server>/carriers/, even empty: how wireguard-tunnels knows the
    server exists (it writes jump files there; wgt-ctl only reads them)."""
    return make_dir(carriers_dir(server))


def drop_anchor(server: str) -> str:
    """Delete servers/<server>/ when nothing but Finder's .DS_Store is in it;
    else where what's left is."""
    top = os.path.dirname(carriers_dir(server))
    if not os.path.isdir(top):
        return ""
    left = [os.path.join(d, f) for d, _dirs, files in os.walk(top) for f in files if f != ".DS_Store"]
    if left:
        return shown(top)
    shutil.rmtree(top)
    return ""


def staged(server: str) -> List[str]:
    """<server>'s staged jump configs (servers/<server>/carriers/*.conf)."""
    d = carriers_dir(server)
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".conf")) if os.path.isdir(d) else []


def config_dir(device: str) -> str:
    return os.path.join(root(), "devices", device, "config")


def make_dir(path: str) -> str:
    """<path> and any missing folders up from it to the root, each 0700."""
    top = root()
    todo = []
    d = path
    while not os.path.isdir(d):
        todo.append(d)
        if d == top or os.path.dirname(d) == d:
            break
        d = os.path.dirname(d)
    for d in reversed(todo):
        remote.private_dir(d)
    return path


def header_of(text: str) -> Optional[Tuple[str, str]]:
    """(config, server) from a config's "# wireguard-tools client:" line."""
    m = HEADER.search(text)
    return (m.group(1), m.group(2)) if m else None


def key_of(text: str) -> str:
    m = PRIVATE_KEY.search(text)
    return m.group(1) if m else ""


@dataclass
class Action:
    """What delivery does with one file. kind: new, update, same, refuse or retire."""

    kind: str
    path: str
    config: str
    text: str = ""
    note: str = ""


def local_files(devices: List[str]) -> Dict[str, str]:
    """Every *.conf in those devices' config/ folders: path -> text."""
    found = {}
    for device in devices:
        d = config_dir(device)
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            p = os.path.join(d, f)
            if f.endswith(".conf") and os.path.isfile(p):
                found[p] = store.read_text(p) or ""
    return found


def all_devices() -> List[str]:
    d = os.path.join(root(), "devices")
    return sorted(f for f in os.listdir(d) if os.path.isdir(os.path.join(d, f))) if os.path.isdir(d) else []


def plan(server: str, issued: Dict[str, str], local: Dict[str, str], complete: bool) -> List[Action]:
    """What to do to bring the local copies of <server>'s configs in line.
    issued: config name -> its text on the server; local: path -> text (devices/*/config/);
    complete: issued is everything the server has, so copies of configs it lacks retire."""
    by_key: Dict[str, str] = {}
    by_header: Dict[Tuple[str, str], str] = {}
    for path, text in local.items():
        k = key_of(text)
        if k:
            by_key.setdefault(k, path)
        h = header_of(text)
        if h:
            by_header.setdefault(h, path)
    actions = []
    claimed = set()
    for config in sorted(issued, key=store.device_key):
        text = issued[config]
        k = key_of(text)
        match = (by_key.get(k) if k else None) or by_header.get((config, server))
        if match and match not in claimed:
            claimed.add(match)
            kind = "same" if local[match] == text else "update"
            device = store.device_of(config)
            moved = os.path.dirname(match) != config_dir(device)
            note = f"left in its folder; the device is now '{device}' (rename the folder to match)" if moved else ""
            actions.append(Action(kind, match, config, text, note))
            continue
        path = os.path.join(config_dir(store.device_of(config)), f"{server}.{config}.conf")
        if path in local or path in claimed:
            actions.append(Action("refuse", path, config, note="a file by that name that isn't this config"))
            continue
        claimed.add(path)
        actions.append(Action("new", path, config, text))
    if complete:
        live_keys = {key_of(t) for t in issued.values()} - {""}
        for path, text in sorted(local.items()):
            h = header_of(text)
            if path in claimed or not h or h[1] != server or key_of(text) in live_keys:
                continue
            actions.append(Action("retire", path, h[0], note="the server no longer has it"))
    return actions


def bak_path(path: str) -> str:
    return f"{path}.{time.strftime('%Y%m%d-%H%M%S')}.bak"


def apply(actions: List[Action]) -> None:
    for a in actions:
        if a.kind in ("new", "update"):
            make_dir(os.path.dirname(a.path))
            if a.kind == "update":
                os.replace(a.path, bak_path(a.path))
            remote.write_local(a.path, a.text)
        elif a.kind == "retire":
            os.replace(a.path, bak_path(a.path))


def shown(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


def report(server: str, actions: List[Action]) -> None:
    words = {"new": "added", "update": "updated (old copy kept as .bak)",
             "retire": "retired to .bak (the server no longer has it)"}
    for a in actions:
        if a.kind in words:
            out.info(f"{server} {a.config}: {words[a.kind]}: {shown(a.path)}")
        elif a.kind == "refuse":
            out.warn(f"{server} {a.config}: not delivered: {shown(a.path)} is {a.note}")
        if a.note and a.kind in ("same", "update"):
            out.warn(f"{server} {a.config}: {shown(a.path)}: {a.note}")


def fetch(s: Server) -> Dict[str, str]:
    """Every client config the server has: config name -> text. The name comes
    from each file's header (the file name also holds the server's own name)."""
    d = remote.REMOTE.client_dir
    rc, o = remote.ssh(s.target, f"cd {remote.q(d)} 2>/dev/null || exit 0; for f in *.conf; do "
                                 f"[ -f \"$f\" ] || continue; printf '{MARK}%s\\n' \"$f\"; cat \"$f\"; done",
                       batch=True, capture=True)
    if rc != 0:
        raise WgtError(f"can't read {s.name}'s client configs" + (" (unreachable)" if rc == 255 else ""))
    issued = {}
    for chunk in o.split(MARK)[1:]:
        file, _, text = chunk.partition("\n")
        h = header_of(text)
        if h:
            issued[h[0]] = text
        else:
            out.warn(f"{s.name}: {file} has no '# wireguard-tools client:' line; not delivered")
    return issued


def server_name(issued: Dict[str, str], s: Server) -> str:
    """The name the server writes in its configs (its own setting; usually the list's name)."""
    names = {h[1] for h in (header_of(t) for t in issued.values()) if h}
    return names.pop() if len(names) == 1 else s.name


def deliver(s: Server) -> int:
    """Copy <s>'s client configs into devices/<device>/config/. 0, or 1 if any
    file was refused or the server couldn't be read."""
    issued = fetch(s)
    anchor(s.name)
    name = server_name(issued, s)
    devices = sorted(set(all_devices()) | {store.device_of(c) for c in issued})
    actions = plan(name, issued, local_files(devices), complete=True)
    apply(actions)
    report(s.name, actions)
    same = sum(a.kind == "same" for a in actions)
    if same:
        out.info(f"{s.name}: {same} config(s) already up to date")
    elif not actions:
        out.info(f"{s.name}: no client configs")
    return 1 if any(a.kind == "refuse" for a in actions) else 0


def do_sync(s: Server, args: List[str]) -> int:
    """`wgt-ctl <server> sync`: deliver its client configs."""
    remote.no_args("sync", args)
    return deliver(s)


CHANGES = {"client": ("add", "rotate", "rename", "remove"), "device": ("rename",)}


def changes_configs(cmd: str, args: List[str]) -> bool:
    """A wgt command that may have changed client configs. `client` alone and
    `client list` count: their action menu can rotate, rename or remove one."""
    if "--dry-run" in args or "--tsv" in args:
        return False
    if cmd == "client" and (not args or args[0] == "list"):
        return True
    return bool(args) and args[0] in CHANGES.get(cmd, ())


def after(s: Server, cmd: str, args: List[str]) -> None:
    """After a wgt command that changed client configs: deliver, warning (not failing) if it can't."""
    if changes_configs(cmd, args):
        try:
            deliver(s)
        except (WgtError, OSError) as e:
            out.warn(f"{s.name}: configs not delivered to {shown(root())}: {e} (wgt-ctl {s.name} sync retries)")
