"""Client configs in ~/wg-tunnels (home.py: the whole layout), and the jump
files staged there. wgt-ctl's folders are made on demand (0700, like the root):

  fleet/servers/<server>/carriers/   jump configs staged for a server (jumpfiles.py)
  devices/<device>/config/     client configs as the servers issued them (below)

Besides those and home.py's, everything under it belongs to wireguard-tunnels.

Delivery: a server's client configs are copied to devices/<device>/config/.
The server's copy is the live one, so it wins over a local copy of the same
config. A local file is the same config when it has the same private key
(rename keeps keys), or else the same "# wireguard-tools client: <config> on
<server>" line. A matched file keeps its name; a new one is <server>.<config>.conf.
A file that would be overwritten but isn't a copy of that config is left alone.
A copy whose config the server no longer has becomes a .bak, as does the old
text of a copy that changes.

warped.* and warp.<device>.conf are wireguard-tunnels' own (its sweep writes
them; store.sweep_owned there): a warped.* is a carrier it swept, which keeps the
server config's key and header line but not its text. Delivery never matches,
updates or retires one, and never writes a file by either name."""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .. import store, system
from ..const import client_conf_name
from ..ctx import SETTINGS_COMMANDS
from ..errors import WgtError
from . import home, remote
from .home import make_dir, root, shown
from .remote import Server, out

HEADER = re.compile(r"(?m)^# wireguard-tools client: (\S+) on (\S+)[ \t]*$")
PRIVATE_KEY = re.compile(r"(?mi)^[ \t]*PrivateKey[ \t]*=[ \t]*(\S+)")
MARK = "\x1e"   # starts each file's name line in a fetch (never in a config)


def carriers_dir(server: str) -> str:
    """Where <server>'s jump configs wait to be pushed."""
    return os.path.join(home.server_dir(server), "carriers")


def anchor(server: str) -> str:
    """fleet/servers/<server>/carriers/, even empty: where wireguard-tunnels'
    nord.sh and you put its jump files (wgt-ctl only reads them)."""
    return make_dir(carriers_dir(server))


def drop_anchor(server: str) -> str:
    """Delete fleet/servers/<server>/ when nothing but Finder's .DS_Store is in it;
    else where what's left is (jump files, backups)."""
    top = os.path.dirname(carriers_dir(server))
    if not os.path.isdir(top):
        return ""
    left = [os.path.join(d, f) for d, _dirs, files in os.walk(top) for f in files if f != ".DS_Store"]
    if left:
        return shown(top)
    shutil.rmtree(top)
    return ""


def staged(server: str) -> List[str]:
    """<server>'s staged jump configs (carriers/*.conf)."""
    d = carriers_dir(server)
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".conf")) if os.path.isdir(d) else []


def config_dir(device: str) -> str:
    return os.path.join(root(), "devices", device, "config")


def header_of(text: str) -> Optional[Tuple[str, str]]:
    """(config, server) from a config's "# wireguard-tools client:" line."""
    m = HEADER.search(text)
    return (m.group(1), m.group(2)) if m else None


def key_of(text: str) -> str:
    m = PRIVATE_KEY.search(text)
    return m.group(1) if m else ""


@dataclass
class Action:
    """What delivery does with one file. kind: new, update, same, refuse, retire, or
    duplicate (another file with a config's key: left alone, with a warning)."""

    kind: str
    path: str
    config: str
    text: str = ""
    note: str = ""


WARPED = "warped."   # wireguard-tunnels' prefix for a swept carrier
NOT_A_COPY = "a file by that name that isn't this config"


def sweep_owned(file: str, device: str) -> bool:
    """A config/ file name wireguard-tunnels' sweep writes: warped.*, or
    warp.<device>.conf (the device's WARP alone). Without case: on a Mac's file
    system Warp.iphone.conf is warp.iphone.conf, and <device> may be another
    spelling of the folder."""
    return file.lower().startswith(WARPED) or same_name(file, f"warp.{device}.conf")


def same_name(a: str, b: str) -> bool:
    """One file's name on a Mac's file system, which ignores case (Warp.iphone.conf is warp.iphone.conf)."""
    return a.lower() == b.lower()


def local_files(devices: List[str]) -> Dict[str, str]:
    """Every *.conf in those devices' config/ folders, the sweep's aside: path -> text.
    Each folder once: on a Mac, devices/iPhone and devices/iphone are one folder, whose
    files listed twice would each be retired twice."""
    found = {}
    seen = set()
    for device in devices:
        d = config_dir(device)
        if not os.path.isdir(d):
            continue
        st = os.stat(d)
        if (st.st_dev, st.st_ino) in seen:
            continue
        seen.add((st.st_dev, st.st_ino))
        for f in sorted(os.listdir(d)):
            p = os.path.join(d, f)
            if f.endswith(".conf") and not sweep_owned(f, device) and os.path.isfile(p):
                found[p] = store.read_text(p) or ""
    return found


def all_devices() -> List[str]:
    d = os.path.join(root(), "devices")
    return sorted(f for f in os.listdir(d) if os.path.isdir(os.path.join(d, f))) if os.path.isdir(d) else []


def own_path(server: str, config: str) -> str:
    """Where a new copy of <config> goes: devices/<device>/config/<server>.<config>.conf."""
    return os.path.join(config_dir(store.device_of(config)), client_conf_name(server, config))


def delivered(path: str, text: str) -> bool:
    """<path> is the name delivery gives a copy of the config its header names: one
    wgt-ctl wrote, not one made by hand (which may sort first, in another device's folder)."""
    h = header_of(text)
    return h is not None and same_name(path, own_path(h[1], h[0]))


def plan(server: str, issued: Dict[str, str], local: Dict[str, str], complete: bool,
         kept_keys: Iterable[str] = ()) -> List[Action]:
    """What to do to bring the local copies of <server>'s configs in line.
    issued: config name -> its text on the server; local: path -> text (devices/*/config/);
    complete: issued is everything the server has, so copies of configs it lacks retire;
    kept_keys: keys of configs the server has but issued lacks (fetch couldn't name them),
    whose copies therefore stay.
    In three passes: each config's copy found (by key, else header), then what retires,
    then new copies, whose names may be ones this delivery retires."""
    live_keys = ({key_of(t) for t in issued.values()} | set(kept_keys)) - {""}
    by_key: Dict[str, List[str]] = {}
    by_header: Dict[Tuple[str, str], str] = {}
    for path, text in local.items():
        k = key_of(text)
        if k:
            by_key.setdefault(k, []).append(path)
        h = header_of(text)
        # A copy holding a live key is that config's (a rename keeps keys): the key
        # match wins, whichever config the loop below reaches first. Of several with
        # one header (a rotate changed the key), the delivered copy is the one.
        if h and k not in live_keys and (h not in by_header or delivered(path, text)):
            by_header[h] = path
    configs = sorted(issued, key=store.device_key)
    actions: List[Action] = []
    matched: Dict[str, str] = {}
    claimed = set()
    for config in configs:
        k = key_of(issued[config])
        # Several files with its key (a copy made by hand): the one at its own name is
        # its copy, else (renamed on the server) the one delivered under its old name;
        # the others are left as they are, with a warning.
        keyed = sorted(by_key.get(k, []) if k else [],
                       key=lambda p: (not same_name(p, own_path(server, config)), not delivered(p, local[p])))
        match = keyed[0] if keyed else by_header.get((config, server))
        if match and match not in claimed:
            claimed.add(match)
            matched[config] = match
            actions += [Action("duplicate", p, config, note=f"holds its key too, not updated ({shown(match)} is)")
                        for p in keyed[1:]]
    retiring = []
    if complete:
        for path, text in sorted(local.items()):
            h = header_of(text)
            if path not in claimed and h and h[1] == server and key_of(text) not in live_keys:
                retiring.append(Action("retire", path, h[0], note="the server no longer has it"))
    gone = {a.path for a in retiring}
    left = {p: t for p, t in local.items() if p not in gone}
    for config in configs:
        text = issued[config]
        device = store.device_of(config)
        match = matched.get(config)
        if match:
            kind = "same" if local[match] == text else "update"
            moved = os.path.dirname(match) != config_dir(device)
            note = f"left in its folder; the device is now '{device}' (rename the folder to match)" if moved else ""
            actions.append(Action(kind, match, config, text, note))
            continue
        path = own_path(server, config)
        # Names compare without case: on a Mac's file system Warp.iphone.conf is warp.iphone.conf.
        if sweep_owned(os.path.basename(path), device):   # server "warp", config "<device>"
            actions.append(Action("refuse", path, config, note="wireguard-tunnels' own name (its sweep writes it)"))
            continue
        # Claimed first: the name of another config's copy, new, already matched or still
        # to be (it holds a live key; a renamed config's copy keeps its name), is another
        # of this server's, not a stranger's.
        other = next((p for p in claimed if same_name(p, path)), "") \
            or next((p for p in left if same_name(p, path) and key_of(left[p]) in live_keys), "")
        if other:
            case = "" if other == path else " (names differing only in case are one file on a Mac)"
            actions.append(Action("refuse", path, config, note=f"also another of this server's configs{case}"))
            continue
        if any(same_name(p, path) for p in left):
            actions.append(Action("refuse", path, config, note=NOT_A_COPY))
            continue
        claimed.add(path)
        actions.append(Action("new", path, config, text))
    return actions + retiring


def refuse_taken(actions: List[Action]) -> List[Action]:
    """A new copy whose name is already on disk is refused too: local_files lists a
    folder once, so one reached by another path (a symlinked device folder) holds
    files plan() saw under that other path, not this one."""
    gone = [os.path.realpath(a.path) for a in actions if a.kind == "retire"]
    for a in actions:
        # Not a name this delivery retires first (apply): it's free by the time it's written.
        if a.kind == "new" and os.path.lexists(a.path) \
                and not any(same_name(g, os.path.realpath(a.path)) for g in gone):
            a.kind, a.text, a.note = "refuse", "", NOT_A_COPY
    return actions


def bak_path(path: str) -> str:
    """<path>.<YYYYmmdd-HHMMSS>.bak, never an earlier copy's name (system.free_bak)."""
    return system.free_bak(path, time.time())


def apply(actions: List[Action]) -> None:
    """Retires first: a new copy may take a name one of them frees (Phone for phone)."""
    for a in sorted(actions, key=lambda a: a.kind != "retire"):
        if a.kind in ("new", "update"):
            make_dir(os.path.dirname(a.path))
            if a.kind == "update":
                # A copy, not a move: if the write fails, the config is still where it was.
                # Written 0600 from the start (copy2 makes it umask-readable first).
                remote.write_local(bak_path(a.path), store.read_text(a.path) or "")
            remote.write_local(a.path, a.text)
        elif a.kind == "retire":
            os.replace(a.path, bak_path(a.path))


def report(server: str, actions: List[Action]) -> None:
    words = {"new": "added", "update": "updated (old copy kept as .bak)",
             "retire": "retired to .bak (the server no longer has it)"}
    for a in actions:
        if a.kind in words:
            out.info(f"{server} {a.config}: {words[a.kind]}: {shown(a.path)}")
        elif a.kind == "refuse":
            out.warn(f"{server} {a.config}: not delivered: {shown(a.path)} is {a.note}")
        elif a.kind == "duplicate":
            out.warn(f"{server} {a.config}: {shown(a.path)} {a.note}")
        if a.note and a.kind in ("same", "update"):
            out.warn(f"{server} {a.config}: {shown(a.path)}: {a.note}")


def fetch(s: Server) -> Tuple[Dict[str, str], str, Set[str]]:
    """Every client config the server has (config name -> text), the server's
    own SERVER_NAME ("" if unset), and the private keys of the files it can't name
    (no header): not delivered, but still the server's, so no copy of one retires.
    A config's name comes from its header (the file name also holds the server's own name)."""
    d = remote.REMOTE.client_dir
    # A non-UTF-8 byte (a jump's label) is kept as it was: copies are the server's files as is,
    # and a decoding error would escape after()'s warning as a traceback.
    rc, o = remote.ssh(s.target, f"sed -n '/^SERVER_NAME=/p' {remote.q(remote.STATE_FILE)} 2>/dev/null | tail -n 1; "
                                 f"cd {remote.q(d)} 2>/dev/null || exit 0; for f in *.conf; do "
                                 f"[ -f \"$f\" ] || continue; printf '{MARK}%s\\n' \"$f\"; cat \"$f\"; done",
                       batch=True, capture=True, errors="surrogateescape")
    if rc != 0:
        raise WgtError(f"can't read {s.name}'s client configs" + (" (unreachable)" if rc == 255 else ""))
    head, *chunks = o.split(MARK)
    issued = {}
    kept = set()
    for chunk in chunks:
        file, _, text = chunk.partition("\n")
        h = header_of(text)
        if h:
            issued[h[0]] = text
        else:
            out.warn(f"{s.name}: {file} has no '# wireguard-tools client:' line; not delivered")
            kept.add(key_of(text))
    return issued, store.state_get(head, "SERVER_NAME").strip(), kept - {""}


def server_name(issued: Dict[str, str], s: Server, own: str = "") -> str:
    """The name the server writes in its configs: the one their headers give, else
    its own SERVER_NAME <own> (with no configs left, its copies still name it),
    else the list's name."""
    names = {h[1] for h in (header_of(t) for t in issued.values()) if h}
    return names.pop() if len(names) == 1 else own or s.name


def deliver(s: Server) -> int:
    """Copy <s>'s client configs into devices/<device>/config/. 0, or 1 if any
    file was refused or the server couldn't be read."""
    issued, own, kept = fetch(s)
    anchor(s.name)
    name = server_name(issued, s, own)
    # Another listed server's name (setup --name <its name>): its copies would retire
    # here, and these be named as its. One target listed twice (the same string) is one
    # server; the same machine under another login or address can't be told apart.
    other = next((x for x in remote.servers() if x.name != s.name and x.target != s.target
                  and same_name(x.name, name)), None)
    if other:
        raise WgtError(f"{s.name}'s configs name the server '{name}', which is {other.name} here: "
                       f"nothing delivered (give {s.name} its own: wgt-ctl {s.name} setup --name <name>; "
                       f"or, if they're one machine, list it once)")
    for config in sorted(c for c, text in issued.items() if not store.is_utf8(text)):
        # Delivered as is all the same (a copy is the server's file): the fix is on the server.
        code = store.client_exit(config) or "<its jump>"
        out.warn(f"{s.name}: {config} isn't UTF-8 (a jump label from before labels were made so), and "
                 f"wireguard-tunnels refuses it. Its '# exit:' line is rewritten by: wgt-ctl {s.name} "
                 f"jump add {code} {remote.REMOTE.jump_source(code)} (or with --label)")
    devices = sorted(set(all_devices()) | {store.device_of(c) for c in issued})
    actions = refuse_taken(plan(name, issued, local_files(devices), complete=True, kept_keys=kept))
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


# jump add: a new label is in its configs' "# exit:" line (exits._refresh_exit_lines).
CHANGES = {"client": ("add", "rotate", "rename", "remove"), "device": ("rename",), "jump": ("add",),
           "exit": ("add",)}
REWRITES_ALL = SETTINGS_COMMANDS   # sync_client_configs: settings and, after --name, every header


def changes_configs(cmd: str, args: List[str]) -> bool:
    """A wgt command that may have changed client configs. `client` alone and
    `client list` count: their action menu can rotate, rename or remove one. setup
    and restore rewrite every config: a copy must follow before its header's old
    server name stops matching (a rotate or remove after `setup --name`)."""
    if remote.dry_run(args) or "--tsv" in remote.wgt_words(args):
        return False
    if cmd in REWRITES_ALL:
        return True
    sub, _ = remote.subcommand(args)   # flags may come first, as wgt reads them
    if cmd == "client" and sub in ("", "list"):
        return True
    return sub in CHANGES.get(cmd, ())


def after(s: Server, cmd: str, args: List[str]) -> None:
    """After a wgt command that changed client configs: deliver, warning (not failing) if it can't."""
    if changes_configs(cmd, args):
        try:
            deliver(s)
        except (WgtError, OSError) as e:
            out.warn(f"{s.name}: configs not delivered to {shown(root())}: {e} (wgt-ctl {s.name} sync retries)")
