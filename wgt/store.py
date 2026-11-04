"""The server's own files, in the formats the bash version wrote (servers
switched over with their files unchanged; keep them compatible):

  wg-tools.state     KEY=value lines; the last one for a key wins
  clients.db         "name ip public-key allowed-ips" (no private keys)
  clients.disabled   one name per line
  exits.db           "code idx label..."
  wg-tools.backups   "path<TAB>original-copy|NEW"

Reading is pure; writes return the new text, and system.Files writes it."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Dict, Iterable, List, Optional, Tuple

from .const import EXIT_MAX
from .errors import WgtError

if TYPE_CHECKING:
    from .const import Paths

Reader = Callable[[str], Optional[str]]

CLIENTS_HEADER = "# wireguard-tools clients: name ip public-key allowed-ips (no private keys)"
EXITS_HEADER = "# wireguard-tools jumps: code idx label (wgt jump add)"

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

EXIT_CODE_RULE = "2-6 of a-z0-9 and -, starting with a letter, not ending in -, not 'warp' or 'host' (e.g. par-fr)"
HOST_EXITS = ("host", "warp")   # --exit words for this server's own way out ("warp": the old spelling)


def valid_exit_code(code: str) -> bool:
    """See EXIT_CODE_RULE (the code names interfaces px-<code>, wx-<code>)."""
    return bool(_EXIT_CODE.fullmatch(code)) and code not in HOST_EXITS


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


def parse_clients(text: Optional[str]) -> List[Client]:
    """clients.db entries; one without a valid IPv4 address is refused."""
    out = []
    for line in _entries(text):
        f = line.split() + ["", "", ""]
        if not re.fullmatch(r"([0-9]{1,3}\.){3}[0-9]{1,3}", f[1]) or not f[2]:
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


def backup_move(text: Optional[str], path: str, new_orig: str) -> str:
    """The backup record with <path>'s original pointed at <new_orig>."""
    lines = [f"{path}\t{new_orig}" if line.partition("\t")[0] == path else line
             for line in (text or "").splitlines()]
    return "".join(line + "\n" for line in lines)


# --- the whole picture, read once ---

@dataclass
class Snapshot:
    """What a command needs to know about the server's files."""

    state: Optional[str]
    clients: List[Client]
    disabled: List[str]
    exits: List[Exit]

    def client(self, name: str) -> Optional[Client]:
        return next((c for c in self.clients if c.name == name), None)

    def exit(self, code: str) -> Optional[Exit]:
        return next((e for e in self.exits if e.code == code), None)

    def exit_clients(self, code: str) -> List[Client]:
        return [c for c in self.clients if client_exit(c.name) == code]

    def is_disabled(self, name: str) -> bool:
        return name in self.disabled

    def warp_off(self, name: str) -> bool:
        """Config <name> has no warp part: it leaves straight out of its exit
        (this server's IP, or its jump's tunnel), never through a WARP of ours."""
        return not name_warp(name)

    def direct_clients(self) -> List[Client]:
        """Host configs without warp (out this server's IP)."""
        return [c for c in self.clients if not c.exit and self.warp_off(c.name)]


def load(paths: Paths, read: Optional[Reader] = None) -> Snapshot:
    """Read the server's files (through <read>, default read_text)."""
    read = read or read_text
    return Snapshot(
        state=read(paths.state),
        clients=parse_clients(read(paths.client_db)),
        disabled=parse_disabled(read(paths.disabled_db)),
        exits=parse_exits(read(paths.exit_db)),
    )


def psk_reader(paths: Paths, read: Optional[Reader] = None) -> Callable[[str], str]:
    """A function: client name -> its preshared key (trailing newlines removed),
    or "" if it has none (as `$(cat psk)` in the bash version)."""
    read = read or read_text

    def psk(name: str) -> str:
        return (read(paths.psk(name)) or "").rstrip("\n")
    return psk
