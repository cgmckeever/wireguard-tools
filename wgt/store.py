"""The server's own files, and their formats (running servers have these files,
so keep them compatible):

  wg-tools.state     KEY=value lines; the last one for a key wins
  clients.db         "name ip public-key allowed-ips" (no private keys)
  clients.disabled   one name per line
  exits.db           "code idx label..."
  devices.db         "device number [groups]" (its static address; who it can reach)
  wg-tools.backups   "path<TAB>original-copy|NEW"

Reading is pure; writes return the new text, and system.Files writes it."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Dict, Iterable, List, Optional, Tuple

from .const import DEVICE_CTL_FIRST, DEVICE_DIRECT_FIRST, DEVICE_DIRECT_LAST, EXIT_MAX
from .errors import WgtError

if TYPE_CHECKING:
    from .const import Paths

Reader = Callable[[str], Optional[str]]

CLIENTS_HEADER = "# wireguard-tools clients: name ip public-key allowed-ips (no private keys)"
EXITS_HEADER = "# wireguard-tools jumps: code idx label (wgt jump add)"
DEVICES_HEADER = "# wireguard-tools devices: device number [group,group] (its static address, who it can reach)"

_EXIT_CODE = re.compile(r"[a-z][a-z0-9-]{0,4}[a-z0-9]")
_CLIENT_BASE = re.compile(r"[A-Za-z0-9_-]{1,32}")


def read_text(path: str) -> Optional[str]:
    """A file's text (None if it doesn't exist). Line endings and any non-UTF-8
    bytes are kept as they are, so comparing and rewriting is exact."""
    try:
        with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
            return f.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise WgtError(f"cannot read {path}: {e.strerror}") from None


def _entries(text: Optional[str]) -> List[str]:
    """Lines that aren't blank or comments."""
    if not text:
        return []
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


# --- state ---

def state_get(text: Optional[str], key: str) -> str:
    """<key>'s value in wg-tools.state ("" if unset; the last line wins)."""
    value = ""
    for line in (text or "").splitlines():
        if line.startswith(key + "="):
            value = line[len(key) + 1:]
    return value


def state_set(text: Optional[str], key: str, value: str) -> str:
    lines = [line for line in (text or "").splitlines() if not line.startswith(key + "=")]
    lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def state_unset(text: Optional[str], key: str) -> str:
    lines = [line for line in (text or "").splitlines() if not line.startswith(key + "=")]
    return "\n".join(lines) + ("\n" if lines else "")


def saved_settings(text: Optional[str], keys: Iterable[str]) -> Dict[str, str]:
    """Saved values for <keys>."""
    return {k: state_get(text, k) for k in keys}


# --- names ---

def is_uint(word: str) -> bool:
    """Plain ASCII digits (str.isdigit also takes "²", which int() refuses)."""
    return bool(re.fullmatch(r"[0-9]+", word))


EXIT_CODE_RULE = "2-6 of a-z0-9 and -, starting with a letter, not ending in -, not 'warp' or 'host' (e.g. par-fr)"
HOST_EXIT = "host"   # the --exit word for this server's own way out


def valid_exit_code(code: str) -> bool:
    """See EXIT_CODE_RULE (the code names interfaces px-<code>, wx-<code>)."""
    return bool(_EXIT_CODE.fullmatch(code)) and code not in (HOST_EXIT, WARP_TAG)


WARP_TAG = "warp"   # the name part of a config that goes through WARP


def split_name(name: str) -> Tuple[str, bool, str]:
    """A config name [warp.][<jump>.]<device> as (jump code or "", WARP on,
    device): warp.par-fr.mbp-1 -> ("par-fr", True, "mbp-1"). Not validated
    (valid_client_name does that)."""
    parts = name.split(".")
    device, pre = parts[-1], parts[:-1]
    warp = bool(pre) and pre[0] == WARP_TAG
    if warp:
        pre = pre[1:]
    return (pre[0] if pre else ""), warp, device


def config_name(code: str, warp: bool, device: str) -> str:
    """The config name for a device's exit and WARP: split_name reversed."""
    return ".".join(([WARP_TAG] if warp else []) + ([code] if code else []) + [device])


def client_exit(name: str) -> str:
    """A config's jump code (from its name), empty for the host."""
    return split_name(name)[0]


def name_warp(name: str) -> bool:
    """A config goes through WARP: its name has the warp part."""
    return split_name(name)[1]


def valid_client_name(name: str) -> Optional[str]:
    """None if <name> is a valid config name ([warp.][<jump>.]<device>), else why not."""
    shape = f"a config name is [warp.][<jump>.]<device>, the device 1-32 of [A-Za-z0-9_-]: '{name}'"
    parts = name.split(".")
    code, warp, device = split_name(name)
    if len(parts) - 1 != bool(code) + warp or not _CLIENT_BASE.fullmatch(device):
        return shape
    if code and not valid_exit_code(code):
        return f"bad jump code in config name '{name}' (see: wgt jump list)"
    return None


# --- clients ---

def device_of(name: str) -> str:
    """The device part of a config name: warp.par-fr.mbp-1 -> mbp-1."""
    return split_name(name)[2]


def device_key(name: str) -> Tuple[str, bool, str, bool]:
    """Sort key grouping configs by device: the host first, then jumps by
    code; within each, the plain one before the WARP one."""
    code, warp, device = split_name(name)
    return device.lower(), bool(code), code, warp


@dataclass
class Client:
    """A clients.db entry: one tunnel config, named [warp.][<jump>.]<device>."""

    name: str
    ip: str
    pub: str
    allowed: str = ""                                   # comma list, no spaces
    raw: str = field(default="", compare=False, repr=False)   # the line as read

    @property
    def exit(self) -> str:
        return client_exit(self.name)

    def line(self) -> str:
        """The clients.db line: as read, or built from the fields."""
        return self.raw or self.fields_line()

    def fields_line(self) -> str:
        return " ".join(x for x in (self.name, self.ip, self.pub, self.allowed) if x)


def _is_ipv4(word: str) -> bool:
    try:
        ipaddress.IPv4Address(word)
    except ValueError:
        return False
    return True


def parse_clients(text: Optional[str]) -> List[Client]:
    """clients.db entries; one without a valid IPv4 address is refused."""
    out = []
    for line in _entries(text):
        f = line.split() + ["", "", ""]
        if not _is_ipv4(f[1]) or not f[2]:
            raise WgtError(f"clients.db: bad line (want 'name ip public-key [allowed-ips]'): {line}")
        out.append(Client(f[0], f[1], f[2], f[3], raw=line))
    return out


def format_clients(lines: Iterable[str]) -> str:
    """clients.db text: the header, then one line per client."""
    return "\n".join([CLIENTS_HEADER, *lines]) + "\n"


def put_client(text: Optional[str], name: str, entry: Optional[Client] = None) -> str:
    """clients.db with <name> removed (other lines kept as they are),
    optionally adding <entry> at the end."""
    lines = [c.line() for c in parse_clients(text) if c.name != name]
    if entry is not None:
        lines.append(entry.fields_line())
    return format_clients(lines)


def parse_disabled(text: Optional[str]) -> List[str]:
    """clients.disabled: one name per line."""
    return [line for line in (text or "").splitlines() if line]


def set_disabled(text: Optional[str], name: str, disabled: bool) -> str:
    """clients.disabled with <name> added (disabled) or dropped."""
    names = [n for n in parse_disabled(text) if n != name]
    if disabled:
        names.append(name)
    return "".join(n + "\n" for n in names)




# --- exits ---

@dataclass
class Exit:
    """An exits.db entry: code, index (tables and fwmark), label."""

    code: str
    idx: int
    label: str
    raw: str = field(default="", compare=False, repr=False)

    def line(self) -> str:
        """The exits.db line: as read, or built from the fields."""
        return self.raw or self.fields_line()

    def fields_line(self) -> str:
        return f"{self.code} {self.idx} {self.label}"

    @property
    def px(self) -> str:
        """Its jump tunnel's interface."""
        return f"px-{self.code}"

    @property
    def wx(self) -> str:
        """Its WARP tunnel's interface (inside the jump one)."""
        return f"wx-{self.code}"

    @property
    def units(self) -> List[str]:
        """Its wg-quick units, in start order (jump first)."""
        return [f"wg-quick@{self.px}", f"wg-quick@{self.wx}"]


def parse_exits(text: Optional[str]) -> List[Exit]:
    """exits.db entries. A bad index is refused: tables derive from it (an
    out-of-range one could even name the main routing table)."""
    out = []
    for line in _entries(text):
        f = line.split()
        if len(f) < 2 or not valid_exit_code(f[0]) or not re.fullmatch(r"[0-9]+", f[1]) \
                or not 1 <= int(f[1]) <= EXIT_MAX:
            raise WgtError(f"exits.db: bad line (want 'code index label', index 1-{EXIT_MAX}): {line}")
        out.append(Exit(f[0], int(f[1]), " ".join(f[2:]), raw=line))
    return out


def put_exit(text: Optional[str], code: str, entry: Optional[Exit] = None) -> str:
    """exits.db with <code> removed (other lines kept as they are),
    optionally adding <entry> at the end."""
    lines = [e.line() for e in parse_exits(text) if e.code != code]
    if entry is not None:
        lines.append(entry.fields_line())
    return "\n".join([EXITS_HEADER, *lines]) + "\n"


# --- devices ---

@dataclass
class Device:
    """A devices.db entry: a device (all its configs) and its static number."""

    name: str
    number: int
    groups: Tuple[str, ...] = ()

    def line(self) -> str:
        return f"{self.name} {self.number}" + (f" {','.join(self.groups)}" if self.groups else "")


_GROUP = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}")


def parse_groups(word: str) -> Tuple[str, ...]:
    """A comma list of group names, each once, in the order given; a bad name is refused."""
    groups = tuple(dict.fromkeys(g.strip() for g in word.split(",") if g.strip()))
    bad = [g for g in groups if not _GROUP.fullmatch(g)]
    if bad or not groups:
        raise WgtError(f"a group name is 1-32 of [A-Za-z0-9_-], starting with a letter: '{word}'")
    return groups


def valid_device_number(number: int) -> bool:
    return DEVICE_CTL_FIRST <= number <= DEVICE_DIRECT_LAST


def parse_devices(text: Optional[str]) -> List[Device]:
    """devices.db entries; a bad number is refused, and so is a name or number
    used twice: one static address can only forward to one device."""
    out: List[Device] = []
    for line in _entries(text):
        f = line.split()
        if len(f) not in (2, 3) or not _CLIENT_BASE.fullmatch(f[0]) or not is_uint(f[1]) \
                or not valid_device_number(int(f[1])):
            raise WgtError(f"devices.db: bad line (want 'device number [groups]', number "
                           f"{DEVICE_CTL_FIRST}-{DEVICE_DIRECT_LAST}): {line}")
        try:
            groups = parse_groups(f[2]) if len(f) == 3 else ()
        except WgtError as e:
            raise WgtError(f"devices.db: bad line ({e}): {line}") from e
        if any(d.name == f[0] or d.number == int(f[1]) for d in out):
            raise WgtError(f"devices.db: that device or number is already listed: {line}")
        out.append(Device(f[0], int(f[1]), groups))
    return out


def put_device(text: Optional[str], name: str, number: Optional[int] = None,
               groups: Optional[Tuple[str, ...]] = None) -> str:
    """devices.db with <name> removed, optionally added back with <number> and
    <groups> (None: the ones it has)."""
    found = parse_devices(text)
    had = next((d.groups for d in found if d.name == name), ())
    entries = [d for d in found if d.name != name]
    if number is not None:
        entries.append(Device(name, number, had if groups is None else groups))
    return "\n".join([DEVICES_HEADER, *(d.line() for d in entries)]) + "\n"


def next_direct_number(devices: Iterable[Device]) -> Optional[int]:
    """The number a `wgt client add` takes: below the lowest in the direct
    range (None if that runs out)."""
    used = [d.number for d in devices if d.number >= DEVICE_DIRECT_FIRST]
    number = min(used) - 1 if used else DEVICE_DIRECT_LAST
    return number if number >= DEVICE_DIRECT_FIRST else None


# --- backup record ---

def backup_orig(text: Optional[str], path: str) -> str:
    """A file's original copy (what rollback restores), NEW if we created it, "" if unknown."""
    for line in (text or "").splitlines():
        recorded, _, orig = line.partition("\t")
        if recorded == path:
            return orig
    return ""


def backup_record(text: Optional[str], path: str, orig: str) -> str:
    """Add <path> -> <orig> unless the file is already recorded."""
    if any(line.partition("\t")[0] == path for line in (text or "").splitlines()):
        return text or ""
    return (text or "") + f"{path}\t{orig}\n"


def backup_forget(text: Optional[str], path: str) -> str:
    """The backup record without <path>."""
    return "".join(line + "\n" for line in (text or "").splitlines() if line.partition("\t")[0] != path)


# --- the whole picture, read once ---

@dataclass
class Snapshot:
    """What a command needs to know about the server's files."""

    state: Optional[str]
    clients: List[Client]
    disabled: List[str]
    exits: List[Exit]
    devices: List[Device] = field(default_factory=list)

    def client(self, name: str) -> Optional[Client]:
        return next((c for c in self.clients if c.name == name), None)

    def exit(self, code: str) -> Optional[Exit]:
        return next((e for e in self.exits if e.code == code), None)

    def exit_clients(self, code: str) -> List[Client]:
        return [c for c in self.clients if client_exit(c.name) == code]

    def device_number(self, name: str) -> Optional[int]:
        """Device <name>'s static number (None if it has none)."""
        return next((d.number for d in self.devices if d.name == name), None)

    def device_groups(self, name: str) -> Tuple[str, ...]:
        """Device <name>'s groups (none: isolated)."""
        return next((d.groups for d in self.devices if d.name == name), ())

    def device_clients(self, name: str) -> List[Client]:
        """Device <name>'s configs."""
        return [c for c in self.clients if device_of(c.name) == name]

    def device_names(self) -> List[str]:
        """Every device with a config, in clients.db order."""
        return list(dict.fromkeys(device_of(c.name) for c in self.clients))

    def is_disabled(self, name: str) -> bool:
        return name in self.disabled

    def direct_clients(self) -> List[Client]:
        """Host configs without warp (out this server's IP)."""
        return [c for c in self.clients if not c.exit and not name_warp(c.name)]


def load(paths: Paths, read: Optional[Reader] = None) -> Snapshot:
    """Read the server's files (through <read>, default read_text)."""
    read = read or read_text
    return Snapshot(
        state=read(paths.state),
        clients=parse_clients(read(paths.client_db)),
        disabled=parse_disabled(read(paths.disabled_db)),
        exits=parse_exits(read(paths.exit_db)),
        devices=parse_devices(read(paths.device_db)),
    )


def psk_reader(paths: Paths, read: Optional[Reader] = None) -> Callable[[str], str]:
    """A function: client name -> its preshared key (trailing newlines removed),
    or "" if it has none."""
    read = read or read_text

    def psk(name: str) -> str:
        return (read(paths.psk(name)) or "").rstrip("\n")
    return psk
