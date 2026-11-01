"""Terminal helpers: ages, numbered menus, prompts."""

from __future__ import annotations

import os
import time
from typing import List, Optional, Sequence


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
    """Read one line from the terminal ("" on EOF or no terminal)."""
    try:
        with open("/dev/tty", "r+") as tty:
            tty.write(prompt)
            tty.flush()
            return tty.readline().strip()
    except OSError:
        return ""


def say(text: str) -> None:
    """Write to the terminal (menus go there even when stdout is captured)."""
    try:
        with open("/dev/tty", "w") as tty:
            tty.write(text)
    except OSError:
        pass


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

