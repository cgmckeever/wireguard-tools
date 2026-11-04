"""Terminal helpers: ages, prompts (yes/no, numbers), numbered menus, and the
clients table's columns (ClientRow; shared by `wgt client list` and
`wgt-ctl clients`)."""

from __future__ import annotations

import time
from typing import List, NamedTuple, Optional, Sequence

from .store import name_warp


def ago(ts: int, now: Optional[float] = None) -> str:
    """Human-readable age of a handshake timestamp."""
    if not ts:
        return "never"
    d = int((now or time.time()) - ts)
    if d < 120:
        return f"{d}s ago"
    if d < 7200:
        return f"{d // 60}m ago"
    return f"{d // 3600}h ago"


def have_tty() -> bool:
    """True if we can prompt on the terminal (even with stdin redirected)."""
    try:
        with open("/dev/tty"):
            return True
    except OSError:
        return False


def ask(prompt: str) -> str:
    """Read one line from the terminal ("" on EOF or no terminal). Opened
    separately for writing and reading: Python can't open /dev/tty "r+" in
    text mode (it isn't seekable), which used to make every prompt answer ""."""
    try:
        with open("/dev/tty", "w") as out, open("/dev/tty") as inp:
            out.write(prompt)
            out.flush()
            return inp.readline().strip()
    except OSError:
        return ""


def say(text: str) -> None:
    """Write to the terminal (menus go there even when stdout is captured)."""
    try:
        with open("/dev/tty", "w") as tty:
            tty.write(text)
    except OSError:
        pass


def yes(question: str, default: bool) -> bool:
    """Ask a yes/no question ("    question [Y/n] "); Enter takes <default>."""
    reply = ask(f"    {question} {'[Y/n]' if default else '[y/N]'} ").lower()
    return default if not reply else reply.startswith("y")


def pick_number(prompt: str, count: int) -> Optional[int]:
    """Ask for a number 1..count; None on Enter. Raises ValueError on anything else."""
    reply = ask(prompt)
    if not reply:
        return None
    if not reply.isdigit() or not 1 <= int(reply) <= count:
        raise ValueError(reply)
    return int(reply)


def menu(title: str, items: Sequence[str], prompt: str = "Choice") -> Optional[int]:
    """A numbered menu on the terminal; returns the 0-based choice, None on Enter."""
    lines: List[str] = ["", f"  {title}"] if title else [""]
    lines += [f"    {i + 1}) {it}" for i, it in enumerate(items)]
    say("\n".join(lines) + "\n")
    n = pick_number(f"  {prompt} [1-{len(items)}, Enter exits]: ", len(items))
    return None if n is None else n - 1



# --- the clients table (wgt client list, wgt-ctl clients) ---

CLIENT_FMT = "{:<24} {:<18} {:<4} {:<15} {:<14} {:<12} {}"
CLIENT_HEAD = ("CONFIG", "EXIT", "WARP", "ADDRESS", "LAST SEEN", "RX/TX MiB", "ALLOWED IPS")


class ClientRow(NamedTuple):
    """One client in the table; `wgt client list --tsv` prints these fields in order."""

    name: str          # config name, e.g. warp.par-fr.mbp-1
    exit: str          # jump code, or host
    ip: str
    seen: str          # "3m ago", "never", "disabled"
    traffic: str       # "rx/tx" MiB
    allowed: str       # "0.0.0.0/0, ::/0"
    exit_label: str = ""   # the jump's full name ("Paris, France")

    def tsv(self) -> str:
        return "\t".join(self)

    @classmethod
    def from_tsv(cls, line: str) -> ClientRow:
        return cls(*(line.split("\t") + [""] * 7)[:7])

    def cells(self) -> Sequence[str]:
        """The table's columns: the config, where it exits (the jump's full
        name), and whether through WARP."""
        return (self.name, self.exit_label or self.exit, "on" if name_warp(self.name) else "off",
                self.ip, self.seen, self.traffic, self.allowed)
