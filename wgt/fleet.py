"""local/devices: wgt-ctl's memory of each device's static number across the
servers, so a device gets the same one everywhere, even with a server
unreachable. The servers hold the numbers that count (devices.db); this is
refreshed from them whenever wgt-ctl reads them. Pure: no ssh, no files.

  <device> <number> [removed <date>]

A removed device keeps its line, and its number isn't handed out again: an old
SSH config can't reach a new machine by accident. Adding it back brings the
number back; `wgt-ctl devices forget` deletes the line."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional

from .const import DEVICE_CTL_FIRST, DEVICE_CTL_LAST, device_ip
from .errors import WgtError

HEADER = "# wgt-ctl devices: device number [removed date] (static address 10.9.8.<number>)"

# server -> device -> its number there (None: it has none yet)
Seen = Mapping[str, Mapping[str, Optional[int]]]


@dataclass
class Entry:
    number: int
    removed: str = ""


def parse(text: Optional[str]) -> Dict[str, Entry]:
    out: Dict[str, Entry] = {}
    for line in (text or "").splitlines():
        f = line.split()
        if not f or f[0].startswith("#"):
            continue
        if len(f) not in (2, 4) or not f[1].isdigit() or (len(f) == 4 and f[2] != "removed"):
            raise WgtError(f"local/devices: bad line (want 'device number [removed date]'): {line}")
        out[f[0]] = Entry(int(f[1]), f[3] if len(f) == 4 else "")
    return out


def format_entries(entries: Mapping[str, Entry]) -> str:
    lines = [f"{d} {e.number}" + (f" removed {e.removed}" if e.removed else "")
             for d, e in sorted(entries.items(), key=lambda kv: kv[1].number)]
    return "\n".join([HEADER, *lines]) + "\n"


def merge(entries: Mapping[str, Entry], seen: Seen, complete: bool, today: str) -> Dict[str, Entry]:
    """<entries> updated from what the servers hold: new devices are added,
    one seen again loses its removed flag. A device on no server is flagged
    removed only when <complete>: an unreachable server may still have it."""
    out = {d: Entry(e.number, e.removed) for d, e in entries.items()}
    present = set()
    for server in sorted(seen):
        for device, number in seen[server].items():
            present.add(device)
            if device in out:
                out[device].removed = ""
            elif number is not None:
                out[device] = Entry(number)
    if complete:
        for device, e in out.items():
            if device not in present and not e.removed:
                e.removed = today
    return out


def next_low(entries: Mapping[str, Entry]) -> int:
    """The next number wgt-ctl hands out: above every one in its range (removed
    devices' included), so a number isn't reused."""
    used = [e.number for e in entries.values() if DEVICE_CTL_FIRST <= e.number <= DEVICE_CTL_LAST]
    number = max(used) + 1 if used else DEVICE_CTL_FIRST
    if number > DEVICE_CTL_LAST:
        raise WgtError(f"no static numbers left ({DEVICE_CTL_FIRST}-{DEVICE_CTL_LAST}); "
                       "free one with: wgt-ctl devices forget <device>")
    return number


def problems(entries: Mapping[str, Entry], seen: Seen) -> List[str]:
    """What doesn't line up, each with its fix."""
    out = []
    owners: Dict[int, List[str]] = {}
    for device, e in sorted(entries.items()):
        owners.setdefault(e.number, []).append(device)
    for owned, devices in sorted(owners.items()):
        if len(devices) > 1:
            out.append(f"{device_ip(owned)} is claimed by {' and '.join(devices)} "
                       f"(fix one: wgt-ctl devices fix <device> <number>)")
    for server in sorted(seen):
        for device, number in sorted(seen[server].items()):
            want = entries[device].number if device in entries else None
            if number is None:
                out.append(f"{device} has no static address on {server} (wgt-ctl devices sync)")
            elif want is not None and number != want:
                out.append(f"{device} is {device_ip(number)} on {server}, but {device_ip(want)} in the fleet "
                           f"(wgt-ctl devices fix {device} {want})")
    return out
