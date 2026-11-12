"""wgt-ctl's view across servers: every config, and each device's static number
(local/devices; the decisions are in fleet.py)."""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

from .. import store, ui
from ..const import device_ip
from ..ctx import VALUE_FLAGS
from ..errors import WgtError
from . import fleet, remote, tunnels
from .remote import out


def cmd_clients() -> None:
    """Every client on every server in one table, then what doesn't line up
    about their static addresses (what it read also updates local/devices)."""
    seen: Dict[str, Dict[str, Optional[int]]] = {}
    fmt = "  {:<10} " + ui.CLIENT_FMT
    print("")
    print(fmt.format("SERVER", *ui.CLIENT_HEAD))
    for s in remote.servers():
        rc, o = remote.ssh(s.target, f"{s.wgt} client list --tsv", batch=True, capture=True)
        if rc != 0:
            note = "unreachable" if rc == 255 else f"client list failed: exit {rc}"
            print(f"  {s.name:<10} ({note})")
            continue
        rows = sorted((ui.ClientRow.from_tsv(line) for line in o.splitlines() if line),
                      key=lambda row: store.device_key(row.name))
        seen[s.name] = {store.device_of(row.name): int(row.static.rsplit(".", 1)[1]) if row.static else None
                        for row in rows}
        if not rows:
            print(f"  {s.name:<10} (no configs)")
        for row in rows:
            print(fmt.format(s.name, *row.cells()))
    print("")
    remote.ensure_local()
    entries = refresh_entries(seen, len(seen) == len(remote.servers()))
    print_problems(entries, seen)


def read_entries() -> Dict[str, fleet.Entry]:
    return fleet.parse(store.read_text(remote.DEVICES))


def write_entries(entries: Dict[str, fleet.Entry]) -> None:
    remote.write_local(remote.DEVICES, fleet.format_entries(entries))


def refresh_entries(seen: fleet.Seen, complete: bool) -> Dict[str, fleet.Entry]:
    """local/devices updated with what the servers answered."""
    before = read_entries()
    entries = fleet.merge(before, seen, complete, time.strftime("%Y-%m-%d"))
    if entries != before:
        write_entries(entries)
    return entries


def sweep() -> Tuple[Dict[str, Dict[str, Optional[int]]], List[str]]:
    """Every server's devices and numbers, and the servers that didn't answer."""
    seen: Dict[str, Dict[str, Optional[int]]] = {}
    missed = []
    for s in remote.servers():
        rc, o = remote.ssh(s.target, f"{s.wgt} device list --tsv", batch=True, capture=True)
        if rc != 0:
            missed.append(s.name)
            continue
        seen[s.name] = {}
        for line in o.splitlines():
            device, number = (line.split("\t") + [""])[:2]   # groups may follow: per server, not read here
            if device:
                seen[s.name][device] = int(number) if store.is_uint(number) else None
    return seen, missed


def print_problems(entries: Dict[str, fleet.Entry], seen: fleet.Seen) -> None:
    for line in fleet.problems(entries, seen):
        out.warn(line)


def numbered_add(args: List[str]) -> Tuple[List[str], str]:
    """`client add` words with the device's fleet-wide number, and the device if
    this just gave it a new number (forget_number: a failed add gives it back). A
    server that can't be reached is covered by local/devices."""
    device = store.device_of(client_name(args))
    if not device or "--device-number" in args or any(a.startswith("--device-number=") for a in args):
        return args, ""
    remote.ensure_local()
    seen, missed = sweep()
    entries = refresh_entries(seen, not missed)
    if missed:
        out.info(f"not read for the static number (local/devices covers them): {', '.join(missed)}")
    known = entries.get(device)
    number = known.number if known else fleet.next_low(entries)
    if not known:
        entries[device] = fleet.Entry(number)
        write_entries(entries)
    return args + ["--device-number", str(number)], "" if known else device


def forget_number(device: str) -> None:
    """Numbers are never reused, so one no add took shouldn't stay taken."""
    entries = read_entries()
    if entries.pop(device, None):
        write_entries(entries)


def rename_everywhere(args: List[str]) -> int:
    """`all client|device rename <device> <new>`: the whole device, on every server."""
    force = "--force" in args
    passed = [a for a in args if a in remote.YES or a == "--dry-run"]
    words = [a for a in args if a != "--force" and a not in passed]
    if len(words) != 2 or any(w.startswith("-") for w in words):
        raise WgtError("usage: wgt-ctl all device rename <device> <new-device> [--force] [--yes] [--dry-run]")
    old, new = words
    if "." in old or "." in new:
        raise WgtError("all client rename takes device names; a config's way out is per server "
                       "(wgt-ctl <server> client rename <config> <new>)")
    seen, missed = sweep()
    if missed and not force:
        raise WgtError(f"no answer from {', '.join(missed)}: nothing renamed. Run again when they're back, or "
                       "--force and re-run it later to finish")
    clash = [name for name, devices in seen.items() if old in devices and new in devices]
    if clash:
        raise WgtError(f"'{new}' already exists next to '{old}' on {', '.join(clash)}: nothing renamed")
    # '<new>' elsewhere is an earlier rename being finished only if it has this device's number.
    numbers = {name: {d[name] for d in seen.values() if d.get(name) is not None} for name in (old, new)}
    if numbers[old] and numbers[new] and numbers[old] != numbers[new]:
        raise WgtError(f"'{new}' is another device, with a different number: nothing renamed")
    holders = [s for s in remote.servers() if old in seen.get(s.name, {})]
    if not holders and not any(new in devices for devices in seen.values()):
        raise WgtError(f"no device '{old}' on any server")
    rc = 0
    for s in holders:
        out.log(s.name)
        one = remote.do_wgt(s, ["device", "rename", old, new, *passed])
        if one == 0:
            tunnels.after(s, "device", ["rename", old, new, *passed])
        rc = one or rc
    remote.ensure_local()
    entries = read_entries()
    if old in entries and rc == 0 and "--dry-run" not in passed:
        moved = entries.pop(old)
        entries.setdefault(new, moved)
        write_entries(entries)
    return rc


def cmd_devices(args: List[str]) -> int:
    """wgt-ctl devices [sync | fix <device> <n> | forget <device>]."""
    remote.ensure_local()
    verb = args[0] if args else "list"
    if verb == "forget" and len(args) == 2:
        return _devices_forget(args[1])
    if verb == "fix" and len(args) == 3 and store.is_uint(args[2]) and store.valid_device_number(int(args[2])):
        return _devices_fix(args[1], int(args[2]))
    if not (verb in ("list", "sync") and len(args) <= 1):
        raise WgtError("usage: wgt-ctl devices [sync | fix <device> <number 2-254> | forget <device>]")
    seen, missed = sweep()
    entries = refresh_entries(seen, not missed)
    rc = _devices_sync(entries, seen) if verb == "sync" else 0
    if verb == "sync":
        seen, missed = sweep()
        entries = refresh_entries(seen, not missed)
    print("")
    print(f"  {'DEVICE':<32} {'STATIC':<14} SERVERS")
    for device, e in sorted(entries.items(), key=lambda kv: kv[1].number):
        where = [server for server in sorted(seen) if device in seen[server]]
        print(f"  {device:<32} {device_ip(e.number):<14} " + (f"removed {e.removed}" if e.removed else " ".join(where)))
    print("")
    print_problems(entries, seen)
    for name in missed:
        out.warn(f"{name} didn't answer")
    return rc


def _devices_sync(entries: Dict[str, fleet.Entry], seen: fleet.Seen) -> int:
    """Give devices without a number theirs, on each server that has them."""
    rc = 0
    for s in remote.servers():
        for device, number in seen.get(s.name, {}).items():
            if number is not None:
                continue
            if device not in entries:
                entries[device] = fleet.Entry(fleet.next_low(entries))
                write_entries(entries)
            rc = remote.do_wgt(s, ["device", "set", device, str(entries[device].number)]) or rc
    return rc


def _devices_fix(device: str, number: int) -> int:
    seen, missed = sweep()
    entries = refresh_entries(seen, not missed)
    owner = next((d for d, e in entries.items() if e.number == number and d != device), "")
    if owner:
        raise WgtError(f"{device_ip(number)} belongs to '{owner}' (wgt-ctl devices fix {owner} <another>, or "
                       f"forget it if it's removed)")
    holders = [s for s in remote.servers() if device in seen.get(s.name, {})]
    if not holders:
        raise WgtError(f"no device '{device}' on the servers that answered")
    rc = 0
    for s in holders:
        rc = remote.do_wgt(s, ["device", "set", device, str(number)]) or rc
    if rc == 0:
        entries[device] = fleet.Entry(number)
        write_entries(entries)
    for name in missed:
        out.warn(f"{name} didn't answer: it still has the old number (run this again when it's back)")
    return rc


def _devices_forget(device: str) -> int:
    seen, missed = sweep()
    entries = refresh_entries(seen, not missed)
    if device not in entries:
        raise WgtError(f"no device '{device}' in {remote.DEVICES}")
    if any(device in devices for devices in seen.values()):
        raise WgtError(f"'{device}' is still on a server (wgt-ctl <server> client remove <config>)")
    if missed:
        raise WgtError(f"no answer from {', '.join(missed)}, which may still have '{device}'")
    del entries[device]
    write_entries(entries)
    return 0


def client_name(args: List[str]) -> str:
    """The client name in `client add ...` words: the first word that is
    neither a flag nor a value flag's value (`--warp off par-fr.mac`)."""
    words = iter(args[1:])
    for a in words:
        if a in VALUE_FLAGS:
            next(words, None)
        elif not a.startswith("-"):
            return a
    return ""
