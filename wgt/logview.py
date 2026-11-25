"""Live views on the terminal: a log as it grows and stepping back through it
(`wgt history --follow`, `--step` for `wgt history` and `wgt log`), and a
table kept up to date (`wgt client list --watch`, and `--alert`: with a bell when
a config connects).

follow() tails a file the way tail -F does (a rotated file is reopened from its
start). step() is a full-screen view that opens at the newest entry and walks
back one entry per key. Its pure parts (keys, window, search, the screen's
text) are kept apart from the terminal so tests can drive them."""

from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap
import time
from typing import Callable, List, Optional, Set, TextIO, Tuple

HELP = "k/Enter older  j newer  u/d page  g/G oldest/newest  /text search back  n/N again  q quit"
SHORT_HELP = "k older  j newer  / search  q quit"

KEYS = {
    b"k": "older", b"\r": "older", b"\n": "older", b" ": "older", b"\x1b[A": "older", b"\x1bOA": "older",
    b"j": "newer", b"\x1b[B": "newer", b"\x1bOB": "newer",
    b"u": "page_older", b"\x1b[5~": "page_older", b"b": "page_older",
    b"d": "page_newer", b"\x1b[6~": "page_newer",
    b"g": "oldest", b"\x1b[H": "oldest", b"\x1b[1~": "oldest", b"\x1bOH": "oldest",
    b"G": "newest", b"\x1b[F": "newest", b"\x1b[4~": "newest", b"\x1bOF": "newest",
    b"/": "search", b"n": "again_older", b"N": "again_newer",
    b"q": "quit", b"Q": "quit", b"\x1b": "quit", b"\x03": "quit", b"\x04": "quit",
}


def follow(path: str, out: TextIO = sys.stdout, poll: float = 1.0,
           stop: Callable[[], bool] = lambda: False) -> None:
    """Print lines as they're appended to <path>, from its current end, until
    Ctrl-C (or stop()). A rotated file (new inode, or shorter than what was read)
    is read again from its start, so no line is missed across a rotation."""
    pos = os.path.getsize(path) if os.path.isfile(path) else 0
    inode = os.stat(path).st_ino if os.path.isfile(path) else None
    partial = ""
    try:
        while not stop():
            try:
                st = os.stat(path)
            except OSError:
                time.sleep(poll)
                continue
            if st.st_ino != inode or st.st_size < pos:
                inode, pos = st.st_ino, 0
            if st.st_size > pos:
                with open(path, encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    data = f.read()
                    pos = f.tell()
                *lines, partial = (partial + data).split("\n")
                for line in lines:
                    out.write(line + "\n")
                out.flush()
            time.sleep(poll)
    except KeyboardInterrupt:
        pass


# --- the step view: pure parts ---

def window(count: int, cur: int, rows: int) -> int:
    """The first entry shown when <rows> fit and entry <cur> is current: it sits
    a third of the way up from the bottom, so the older ones (where the walk is
    going) are in view."""
    if count <= rows:
        return 0
    start = cur - (rows - rows // 3) + 1
    return max(0, min(start, count - rows))


def search(lines: List[str], cur: int, text: str, older: bool) -> Optional[int]:
    """The next entry from <cur> (older: towards the start) containing <text>,
    case ignored; None if there's none."""
    t = text.lower()
    rng = range(cur - 1, -1, -1) if older else range(cur + 1, len(lines))
    return next((i for i in rng if t in lines[i].lower()), None)


def move(action: str, cur: int, count: int, page: int) -> int:
    """Where <action> takes the current entry."""
    to = {"older": cur - 1, "newer": cur + 1, "page_older": cur - page, "page_newer": cur + page,
          "oldest": 0, "newest": count - 1}.get(action, cur)
    return max(0, min(to, count - 1))


def screen(lines: List[str], cur: int, title: str, cols: int, rows: int, note: str = "") -> str:
    """The step view's whole screen: entries one per row, cut to the width, the
    current one in reverse video; below them the current entry in full, then
    where it is and the keys."""
    full = textwrap.wrap(lines[cur], cols - 2, subsequent_indent="  ") or [""]
    full = full[:max(1, rows // 3)]
    listed = max(1, rows - len(full) - 3)
    start = window(len(lines), cur, listed)
    out = []
    for i in range(start, min(start + listed, len(lines))):
        text = lines[i][:cols - 1]
        out.append(f"\x1b[7m{text.ljust(cols - 1)}\x1b[0m" if i == cur else text)
    out += [""] * (listed - len(out))
    out.append("\x1b[2m" + "-" * (cols - 1) + "\x1b[0m")
    out += full
    where = f"{title}: {cur + 1} of {len(lines)}" + (f"  ({note})" if note else "")
    out.append(f"\x1b[1m{where}\x1b[0m")
    keys = next((h for h in (HELP, SHORT_HELP) if len(h) < cols), "")
    out.append("\x1b[2m" + keys + "\x1b[0m")
    return "\x1b[H\x1b[2J" + "\r\n".join(out)


# --- the step view: the terminal ---

def step(lines: List[str], title: str) -> None:
    """Walk <lines> (oldest first) from the newest back, on the terminal, until q."""
    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    w = sys.stdout.write
    cur, note, last = len(lines) - 1, "", ""
    w("\x1b[?1049h\x1b[?25l")
    try:
        tty.setcbreak(fd)
        while True:
            cols, rows = shutil.get_terminal_size((100, 30))
            w(screen(lines, cur, title, cols, rows, note))
            sys.stdout.flush()
            note = ""
            action = KEYS.get(os.read(fd, 16), "")
            if action == "quit":
                return
            if action == "search":
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)
                w(f"\x1b[{rows};1H\x1b[2K\x1b[?25l/\x1b[?25h")
                sys.stdout.flush()
                last = sys.stdin.readline().strip() or last
                tty.setcbreak(fd)
                w("\x1b[?25l")
                action = "again_older"
            if action in ("again_older", "again_newer"):
                if not last:
                    note = "no search yet: / then the text"
                    continue
                found = search(lines, cur, last, action == "again_older")
                if found is None:
                    note = f"no {'older' if action == 'again_older' else 'newer'} entry has '{last}'"
                else:
                    cur = found
                continue
            cur = move(action, cur, len(lines), max(1, rows // 2))
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        w("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()


class Alerts:
    """For --alert: which names connected (became active) since the last look, kept
    as a few lines to show under the table. The first look only learns."""
    KEEP = 5

    def __init__(self) -> None:
        self.active: Optional[Set[str]] = None
        self.lines: List[str] = []

    def update(self, active: Set[str], stamp: str) -> List[str]:
        """Note <active> now; the names that connected (ring for them), oldest look excluded."""
        new = sorted(active - self.active) if self.active is not None else []
        gone = sorted(self.active - active) if self.active is not None else []
        self.active = set(active)
        self.lines = ([f"{stamp} {n} connected" for n in new] + [f"{stamp} {n} went idle" for n in gone]
                      + self.lines)[:self.KEEP]
        return new


def notice(text: str) -> str:
    """<text> as a desktop notification request (OSC 9): wgt-ctl turns it into one
    on your machine, and terminals that know it (iTerm2, kitty, WezTerm) show it
    themselves; the rest ignore it."""
    return "\x1b]9;" + text.replace("\x07", "").replace("\x1b", "") + "\x07"


_NOTICE = re.compile(rb"\x1b\]9;([^\x07\x1b]*)(?:\x07|\x1b\\)")


def split_notices(data: bytes) -> Tuple[bytes, List[str], bytes]:
    """<data> (a stream's next bytes) without its notices: (what to show, the
    notices' texts, a notice's unfinished start to prepend to the next bytes)."""
    rest = b""
    start = data.rfind(b"\x1b]9;")
    if start != -1 and not _NOTICE.match(data, start) and len(data) - start < 1024:
        data, rest = data[:start], data[start:]
    texts = [m.group(1).decode("utf-8", "replace") for m in _NOTICE.finditer(data)]
    return _NOTICE.sub(b"", data), texts, rest


def watch(draw: Callable[[], str], title: str, every: float = 2.0) -> None:
    """<draw>'s text, redrawn every <every> seconds (any key: now) until q or Ctrl-C."""
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    w = sys.stdout.write
    w("\x1b[?1049h\x1b[?25l")
    try:
        tty.setcbreak(fd)
        while True:
            body = draw()
            stamp = time.strftime("%H:%M:%S")
            w("\x1b[H\x1b[2J" + body.replace("\n", "\r\n")
              + f"\r\n\r\n\x1b[2m{title}, {stamp}: every {every:g}s (any key: now)  q quit\x1b[0m")
            sys.stdout.flush()
            if select.select([fd], [], [], every)[0] and KEYS.get(os.read(fd, 16)) == "quit":
                return
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        w("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()


def can_step() -> bool:
    """A person at a terminal: step needs one for both reading keys and drawing."""
    return sys.stdin.isatty() and sys.stdout.isatty()
