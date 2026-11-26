"""fleet/device-list.json: wgt-ctl's memory of each device's static number across the
servers, so a device gets the same one everywhere, even with a server
unreachable. The servers hold the numbers that count (devices.db); this is
refreshed from them whenever wgt-ctl reads them. Pure: no ssh, no files.

  {
    "phone": {"number": 3},                          its static address ends .3 everywhere
    "tab": {"number": 252, "removed": "2026-11-24"}   gone from every server that day
  }

A removed device keeps its entry, and its number isn't handed out again: an old
SSH config can't reach a new machine by accident. Adding it back brings the
number back; `wgt-ctl devices forget` deletes the entry."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional

from ..const import DEVICE_CTL_FIRST, DEVICE_CTL_LAST, device_ip
from ..errors import WgtError

NAME = "fleet/device-list.json"
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

# server -> device -> its number there (None: it has none yet)
Seen = Mapping[str, Mapping[str, Optional[int]]]


@dataclass
class Entry:
    number: int
    removed: str = ""


def _good_entry(device: str, v: object) -> bool:
    if not device or not isinstance(v, dict) or set(v) - {"number", "removed"}:
        return False
    number, removed = v.get("number"), v.get("removed", "")
    return (type(number) is int and number >= 0 and isinstance(removed, str)
            and (removed == "" or bool(DATE.fullmatch(removed))))


def parse(text: Optional[str]) -> Dict[str, Entry]:
    if not text or not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as e:
        raise WgtError(f"{NAME}: not JSON ({e})") from None
    if not isinstance(data, dict):
        raise WgtError(f"{NAME}: want an object of devices")
    out: Dict[str, Entry] = {}
    for device, v in data.items():
        if not _good_entry(device, v):
            raise WgtError(f'{NAME}: bad entry for {device!r} (want {{"number": N}}, '
                           f'and "removed": "YYYY-MM-DD" once gone): {json.dumps(v)}')
        out[device] = Entry(v["number"], v.get("removed", ""))
    return out


def format_entries(entries: Mapping[str, Entry]) -> str:
    data = {d: {"number": e.number, **({"removed": e.removed} if e.removed else {})}
            for d, e in sorted(entries.items(), key=lambda kv: kv[1].number)}
    return json.dumps(data, indent=2) + "\n"


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
