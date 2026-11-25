"""Desktop notifications on this machine, for wgt-ctl's --alert views: macOS
(osascript, with a sound) or Linux (notify-send); nothing where there's neither
(the terminal's bell still rings)."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from typing import Callable, List, Optional

from .. import logview

SOUND = "Glass"


def command(title: str, text: str) -> Optional[List[str]]:
    """The command that shows <text> under <title> here, or None."""
    if sys.platform == "darwin" and shutil.which("osascript"):
        script = ("on run argv\n"
                  f'display notification (item 2 of argv) with title (item 1 of argv) sound name "{SOUND}"\n'
                  "end run")
        return ["osascript", "-e", script, title, text]
    if shutil.which("notify-send"):
        return ["notify-send", title, text]
    return None


def desktop(text: str, title: str = "wgt") -> None:
    """Show <text>, without waiting for it and never failing the view."""
    cmd = command(title, text)
    if cmd:
        with contextlib.suppress(OSError):
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def pump(fd: int, show: Callable[[str], None] = desktop) -> None:
    """Copy a stream (fd) to stdout as it comes, with its notices taken out and shown."""
    rest = b""
    while True:
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        data, texts, rest = logview.split_notices(rest + chunk)
        os.write(sys.stdout.fileno(), data)
        for t in texts:
            show(t)
    if rest:
        os.write(sys.stdout.fileno(), rest)
