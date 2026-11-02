"""Everything that touches the machine: running commands (dry-run aware),
output, and writing files with backups. Tests swap Runner for FakeRunner,
which records commands instead of running them."""

from __future__ import annotations

import contextlib
import difflib
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Callable, Dict, List, Optional, Sequence, TextIO, Tuple

from . import store
from .const import KEEP_CHANGES, LOG_TAG, Paths
from .errors import WgtError

_SECRET = re.compile(r"^(\s*(PrivateKey|PresharedKey)\s*=\s*).*$", re.M)


def atomic_write(path: str, content: str, mode: int) -> None:
    """Write <path> atomically: a private temp file in the same directory
    (created 0600, so a secret is never readable on the way), mode set, synced,
    then renamed over the target."""
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".wg-tools.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write(content)
            f.flush()
            os.fchmod(f.fileno(), mode)
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def settings_of(text: str) -> list:
    """A config's lines without comments and blank lines."""
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def write_private(path: str, content: str) -> None:
    """A root-only file (mode 600), written atomically. Used where Files.write
    is deliberately skipped: the state file and server keys (no change copies
    of keys; rollback reads the state rather than restoring it)."""
    atomic_write(path, content, 0o600)


# --- marker files in /run (the watchdogs' and exit tests' small notes) ---
# Written directly: they aren't configuration, so no backups and no dry-run.

def write_marker(path: str, value: str = "") -> None:
    with open(path, "w") as f:
        f.write(value)


def read_marker(path: str) -> int:
    """A marker's number (a timestamp), or 0 if it's missing or not a number."""
    try:
        with open(path) as f:
            value = f.read().strip()
    except OSError:
        return 0
    return int(value) if value.isdigit() else 0


def remove_markers(*paths: str) -> None:
    for path in paths:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)


def is_copy_of(name: str, base: str) -> bool:
    """True for <base>.<anything>.bak (a change copy), as the shell glob
    "<base>.*.bak" matched; a plain <base>.bak someone made by hand is not one.
    A <base> containing glob characters matches nothing: those would be read
    as a pattern (and no file we manage has them)."""
    return fnmatch.fnmatchcase(name, base + ".*.bak") and not fnmatch.fnmatchcase(base, "*[*?[]*")


def is_secret(path: str, paths: Paths) -> bool:
    """Files whose whole content is a secret (dry-run diffs must not show them)."""
    return path.startswith(paths.psk_dir + "/") or path.endswith((".key", ".psk"))


def redact(text: str) -> str:
    """Strip key values so dry-run diffs and `show` never print secrets."""
    return _SECRET.sub(r"\1<redacted>", text)


class Out:
    """Output in the tools' style: `==> step`, indented info, WARN/ERROR on stderr."""

    def __init__(self, stream: Optional[TextIO] = None, err: Optional[TextIO] = None) -> None:
        self.stream = stream or sys.stdout
        self.err = err or sys.stderr

    def log(self, msg: str) -> None:
        print(f"==> {msg}", file=self.stream, flush=True)

    def info(self, msg: str) -> None:
        print(f"    {msg}", file=self.stream, flush=True)

    def line(self, text: str = "") -> None:
        """A line as is (tables, blank lines between sections)."""
        print(text, file=self.stream, flush=True)

    def warn(self, msg: str) -> None:
        print(f"WARN: {msg}", file=self.err, flush=True)

    def error(self, msg: str) -> None:
        print(f"ERROR: {msg}", file=self.err, flush=True)


def journal(runner: Runner, tag: str, msg: str) -> None:
    """One line in the journal under <tag> (what `wgt log` shows)."""
    runner.ok(["logger", "-t", tag, msg])


class Runner:
    """Runs commands. Mutating commands (run/sh) only print under --dry-run;
    queries (query/ok) always run, since they change nothing."""

    def __init__(self, dry_run: bool = False, out: Optional[Out] = None, prog: str = "wgt"):
        self.dry_run = dry_run
        self.out = out or Out()
        self.prog = prog
        self.current_step = "startup"

    # --- changes ---

    def run(self, args: Sequence[str], check: bool = True, input: Optional[str] = None,
            capture: bool = False) -> Tuple[int, str]:
        if self.dry_run:
            self.out.info("[dry-run] " + " ".join(args))
            return 0, ""
        return self._exec(args, check=check, input=input, capture=capture)

    def sh(self, line: str, check: bool = True) -> int:
        """A shell line (the rule lines are shell)."""
        return self.run(["bash", "-c", line], check=check)[0]

    def run_logged(self, args: Sequence[str], log: str, cwd: str, append: bool = False) -> bool:
        """Run a command in <cwd> with its output going to <log> (umask 077: it
        writes keys there); True if it succeeded. For wgcf, whose output is
        only needed when it fails."""
        if self.dry_run:
            self.out.info(f"[dry-run] (in {cwd}) " + " ".join(args))
            return True
        old = os.umask(0o077)
        try:
            with open(log, "a" if append else "w") as f:
                return subprocess.run(list(args), cwd=cwd, stdout=f, stderr=f).returncode == 0
        except OSError:
            return False
        finally:
            os.umask(old)

    def sh_lines(self, lines: Sequence[str]) -> None:
        for line in lines:
            self.sh(line)

    # --- queries ---

    def query(self, args: Sequence[str], input: Optional[str] = None) -> str:
        """A read-only command's stdout ("" if it fails or can't run)."""
        try:
            return self._exec(args, check=False, input=input, capture=True)[1]
        except OSError:
            return ""

    def ok(self, args: Sequence[str]) -> bool:
        """True if a command succeeds (False if it fails or can't run)."""
        try:
            return self._exec(args, check=False, capture=True)[0] == 0
        except OSError:
            return False

    def _exec(self, args: Sequence[str], check: bool, input: Optional[str] = None,
              capture: bool = False) -> Tuple[int, str]:
        try:
            p = subprocess.run(list(args), input=input, text=True, errors="replace",
                               stdout=subprocess.PIPE if capture else None,
                               stderr=subprocess.PIPE if capture else None)
        except OSError as e:
            if not check:
                raise
            raise WgtError(f"cannot run {args[0]}: {e.strerror}") from None
        if check and p.returncode != 0:
            why = (p.stderr or "").strip().splitlines()[-1:] if capture else []
            raise WgtError(f"command failed (exit {p.returncode}): {' '.join(args)}" + (f": {why[0]}" if why else ""))
        return p.returncode, p.stdout or ""

    # --- steps ---

    def step(self, what: str, undo: str = "") -> None:
        """Announce a change and how to undo it; record it in the journal."""
        self.current_step = what
        self.out.log(what)
        if undo:
            self.out.info(f"undo: {undo}")
        if not self.dry_run:
            journal(self, LOG_TAG, f"{self.prog}: {what}")


class FakeRunner(Runner):
    """For tests: records commands instead of running them. `answers` maps a
    command (as a joined string, or a prefix ending in '*') to (returncode, stdout)."""

    def __init__(self, answers: Optional[Dict[str, Tuple[int, str]]] = None, dry_run: bool = False,
                 out: Optional[Out] = None):
        super().__init__(dry_run=dry_run, out=out)
        self.answers = answers or {}
        self.calls: List[str] = []

    def _exec(self, args: Sequence[str], check: bool, input: Optional[str] = None,
              capture: bool = False) -> Tuple[int, str]:
        cmd = " ".join(args)
        self.calls.append(cmd)
        rc, stdout = self.answers.get(cmd, (None, None))
        if rc is None:
            for k, v in self.answers.items():
                if k.endswith("*") and cmd.startswith(k[:-1]):
                    rc, stdout = v
                    break
        if rc is None:
            rc, stdout = 0, ""
        if check and rc != 0:
            raise WgtError(f"command failed (exit {rc}): {cmd}")
        return rc, stdout or ""

    def run_logged(self, args: Sequence[str], log: str, cwd: str, append: bool = False) -> bool:
        return self._exec([*args], check=False)[0] == 0

    def changes(self) -> List[str]:
        """The recorded commands, without logger lines."""
        return [c for c in self.calls if not c.startswith("logger ")]


class Files:
    """File writes with backups. Before a file changes, a timestamped copy goes
    to <backup_dir>/files/<its path>; the first copy (or NEW, if we created
    it) is recorded so rollback can restore it. Each file keeps its original
    plus the KEEP_CHANGES newest copies."""

    def __init__(self, paths: Paths, runner: Runner, clock: Callable[[], float] = time.time):
        self.paths = paths
        self.runner = runner
        self.clock = clock
        # A dry run writes nothing, but later steps should see what it would
        # have written (so e.g. wg0.conf's diff after a client change shows):
        # path -> content, or None for a file it would have removed.
        self.pending: Dict[str, Optional[str]] = {}

    def read(self, path: str) -> Optional[str]:
        """A file's text as this run sees it (including a dry run's would-be writes)."""
        if path in self.pending:
            return self.pending[path]
        return store.read_text(path)

    @property
    def out(self) -> Out:
        return self.runner.out

    def change_dir(self, path: str) -> str:
        """Where <path>'s change copies go: files/ plus its own directory."""
        return self.copies_dir(os.path.dirname(os.path.abspath(path)))

    def copies_dir(self, directory: str) -> str:
        """Where change copies of files in <directory> go (files/ + <directory>)."""
        return self.paths.change_dir + os.path.abspath(directory)

    def write(self, path: str, mode: int, content: str) -> bool:
        """Write <content> to <path> if it differs (atomically, after a backup).
        Returns True if it changed; a dry run prints a redacted diff instead."""
        old = self.read(path)
        if old == content:
            self.out.info(f"unchanged: {path}")
            return False
        if self.runner.dry_run:
            if is_secret(path, self.paths):
                self.out.info(f"[dry-run] would write {path} (mode {mode:o}; secret, not shown)")
                self.pending[path] = content
                return True
            self.out.info(f"[dry-run] would write {path} (mode {mode:o}):")
            diff = difflib.unified_diff(redact(old or "").splitlines(True), redact(content).splitlines(True),
                                        fromfile=path, tofile=f"{path} (new)")
            for line in diff:
                self.out.info("  " + line.rstrip("\n"))
            self.pending[path] = content
            return True
        self.backup(path)
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
        atomic_write(path, content, mode)
        self.out.info(f"wrote: {path}")
        return True

    def backup(self, path: str) -> None:
        """Copy <path> aside before it changes (a dry run copies nothing)."""
        if self.runner.dry_run:
            return
        orig = "NEW"
        if os.path.exists(path):
            d = self.change_dir(path)
            os.makedirs(self.paths.backup_dir, mode=0o700, exist_ok=True)
            os.chmod(self.paths.backup_dir, 0o700)
            os.makedirs(d, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock()))
            bak = os.path.join(d, f"{os.path.basename(path)}.{stamp}.bak")
            while os.path.exists(bak):   # two changes within the same second
                bak = bak[: -len(".bak")] + f".{os.getpid() % 32768}.bak"
            shutil.copy2(path, bak)
            self.out.info(f"backup: {bak}")
            orig = bak
        os.makedirs(self.paths.wg_dir, mode=0o700, exist_ok=True)
        rec = store.read_text(self.paths.backup_map)
        new = store.backup_record(rec, path, orig)
        if new != (rec or ""):
            write_private(self.paths.backup_map, new)
        self.prune(path)

    def prune(self, path: str) -> None:
        """Keep <path>'s original copy and its KEEP_CHANGES newest change copies."""
        if self.runner.dry_run:
            return
        orig = store.backup_orig(store.read_text(self.paths.backup_map), path)
        copies_dir = self.change_dir(path)
        if not os.path.isdir(copies_dir):
            return
        base = os.path.basename(path)
        copies = sorted(os.path.join(copies_dir, n) for n in os.listdir(copies_dir) if is_copy_of(n, base))
        kept = 0
        for copy in reversed(copies):   # names end in a timestamp: newest last
            if copy == orig:
                continue
            kept += 1
            if kept > KEEP_CHANGES:
                os.remove(copy)

    def write_config(self, path: str, mode: int, content: str) -> bool:
        """Write a tunnel config; True only if its settings changed. A change in
        comments alone is written but needs no restart (wg-quick reads the file
        only when it starts)."""
        old = self.read(path)
        self.write(path, mode, content)
        return old is None or settings_of(old) != settings_of(content)

    def remove(self, *paths: str) -> None:
        """Delete files (missing ones are fine); a dry run only says so."""
        targets = [p for p in paths if os.path.lexists(p) and not os.path.isdir(p)]
        if not targets:
            return
        if self.runner.dry_run:
            self.out.info("[dry-run] rm -f " + " ".join(targets))
            for p in targets:
                self.pending[p] = None
            return
        for p in targets:
            os.remove(p)

    def remove_empty_dir(self, path: str) -> None:
        """Remove a directory if it exists and is empty; a dry run only says so."""
        if not os.path.isdir(path) or os.listdir(path):
            return
        if self.runner.dry_run:
            self.out.info(f"[dry-run] rmdir {path}")
            return
        os.rmdir(path)

    def remove_tree(self, path: str) -> None:
        if not os.path.isdir(path):
            return
        if self.runner.dry_run:
            self.out.info(f"[dry-run] rm -rf {path}")
            return
        shutil.rmtree(path)

    def forget(self, path: str) -> None:
        """Delete a file these tools created, its change copies and its backup
        record (the copies may hold keys; rollback has nothing to restore)."""
        d = self.change_dir(path)
        targets = [path]
        if os.path.isdir(d):
            base = os.path.basename(path)
            targets += [os.path.join(d, n) for n in os.listdir(d) if is_copy_of(n, base)]
        self.remove(*targets)
        if not self.runner.dry_run:
            rec = store.read_text(self.paths.backup_map)
            if rec is not None:
                write_private(self.paths.backup_map, store.backup_forget(rec, path))

    def restore_original(self, path: str, remove_new: bool) -> None:
        """Restore a file's original copy; files we created are removed if <remove_new>."""
        orig = store.backup_orig(store.read_text(self.paths.backup_map), path)
        if not orig:
            return
        if orig == "NEW":
            if remove_new:
                self.remove(path)
            elif os.path.exists(path):
                self.out.info(f"kept {path} (created by wireguard-tools; --purge deletes it)")
        elif os.path.exists(orig):
            if self.runner.dry_run:
                self.out.info(f"[dry-run] would restore {path} from {orig}")
            else:
                shutil.copy2(orig, path)
                self.out.info(f"restored {path} from {orig}")
        else:
            self.out.warn(f"original backup {orig} is missing; left {path} as is")

