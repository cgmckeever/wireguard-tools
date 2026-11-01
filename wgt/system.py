"""Everything that touches the machine: running commands (dry-run aware),
output, and writing files with backups. Tests swap Runner for FakeRunner,
which records commands instead of running them."""

from __future__ import annotations

import difflib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import store
from .const import KEEP_CHANGES, Paths
from .errors import WgtError

_SECRET = re.compile(r"^(\s*(PrivateKey|PresharedKey)\s*=\s*).*$", re.M)


def redact(text: str) -> str:
    """Strip key values so dry-run diffs and `show` never print secrets."""
    return _SECRET.sub(r"\1<redacted>", text)


class Out:
    """Output in the tools' style: `==> step`, indented info, WARN/ERROR on stderr."""

    def __init__(self, stream=None, err=None):
        self.stream = stream or sys.stdout
        self.err = err or sys.stderr

    def log(self, msg: str) -> None:
        print(f"==> {msg}", file=self.stream, flush=True)

    def info(self, msg: str) -> None:
        print(f"    {msg}", file=self.stream, flush=True)

    def warn(self, msg: str) -> None:
        print(f"WARN: {msg}", file=self.err, flush=True)

    def error(self, msg: str) -> None:
        print(f"ERROR: {msg}", file=self.err, flush=True)


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

    def sh_lines(self, lines: Sequence[str]) -> None:
        for line in lines:
            self.sh(line)

    # --- queries ---

    def query(self, args: Sequence[str], input: Optional[str] = None) -> str:
        """A read-only command's stdout ("" if it fails or doesn't exist)."""
        try:
            return self._exec(args, check=False, input=input, capture=True)[1]
        except FileNotFoundError:
            return ""

    def ok(self, args: Sequence[str]) -> bool:
        try:
            return self._exec(args, check=False, capture=True)[0] == 0
        except FileNotFoundError:
            return False

    def _exec(self, args, check, input=None, capture=False):
        p = subprocess.run(list(args), input=input, text=True,
                           stdout=subprocess.PIPE if capture else None,
                           stderr=subprocess.DEVNULL if capture else None)
        if check and p.returncode != 0:
            raise WgtError(f"command failed (exit {p.returncode}): {' '.join(args)}")
        return p.returncode, p.stdout or ""

    # --- steps ---

    def step(self, what: str, undo: str = "") -> None:
        """Announce a change and how to undo it; record it in the journal."""
        self.current_step = what
        self.out.log(what)
        if undo:
            self.out.info(f"undo: {undo}")
        if not self.dry_run:
            self.ok(["logger", "-t", "wg-tools", f"{self.prog}: {what}"])


class FakeRunner(Runner):
    """Records commands instead of running them. `answers` maps a command (as
    a joined string, or a prefix ending in '*') to (returncode, stdout)."""

    def __init__(self, answers: Optional[Dict[str, Tuple[int, str]]] = None, dry_run: bool = False,
                 out: Optional[Out] = None):
        super().__init__(dry_run=dry_run, out=out)
        self.answers = answers or {}
        self.calls: List[str] = []

    def _exec(self, args, check, input=None, capture=False):
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
        return rc, stdout

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
        self.wrote = False

    @property
    def out(self) -> Out:
        return self.runner.out

    def change_dir(self, path: str) -> str:
        return self.paths.change_dir + os.path.dirname(path)

    def write(self, path: str, mode: int, content: str) -> bool:
        """Write <content> to <path> if it differs (atomically, after a backup).
        Returns True if it changed; a dry run prints a redacted diff instead."""
        old = store.read_text(path)
        self.wrote = False
        if old == content:
            self.out.info(f"unchanged: {path}")
            return False
        self.wrote = True
        if self.runner.dry_run:
            self.out.info(f"[dry-run] would write {path} (mode {mode:o}):")
            diff = difflib.unified_diff(redact(old or "").splitlines(True), redact(content).splitlines(True),
                                        fromfile=path, tofile=f"{path} (new)")
            for line in diff:
                self.out.info("  " + line.rstrip("\n"))
            return True
        self.backup(path)
        os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
        tmp = path + ".wg-tools.new"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        self.out.info(f"wrote: {path}")
        return True

    def backup(self, path: str) -> None:
        orig = "NEW"
        if os.path.exists(path):
            d = self.change_dir(path)
            os.makedirs(self.paths.backup_dir, mode=0o700, exist_ok=True)
            os.chmod(self.paths.backup_dir, 0o700)
            os.makedirs(d, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock()))
            bak = os.path.join(d, f"{os.path.basename(path)}.{stamp}.bak")
            while os.path.exists(bak):
                bak = bak[: -len(".bak")] + f".{os.getpid() % 32768}.bak"
            shutil.copy2(path, bak)
            self.out.info(f"backup: {bak}")
            orig = bak
        os.makedirs(self.paths.wg_dir, mode=0o700, exist_ok=True)
        rec = store.read_text(self.paths.backup_map)
        new = store.backup_record(rec, path, orig)
        if new != (rec or ""):
            self._write_private(self.paths.backup_map, new)
        self.prune(path)

    def prune(self, path: str) -> None:
        """Keep <path>'s original copy and its KEEP_CHANGES newest; copies older
        versions left next to the file are moved (the original) or deleted."""
        if self.runner.dry_run:
            return
        orig = store.backup_orig(store.read_text(self.paths.backup_map), path)
        d = self.change_dir(path)
        base = os.path.basename(path)
        parent = os.path.dirname(path)
        if os.path.isdir(parent):
            for name in sorted(os.listdir(parent)):
                b = os.path.join(parent, name)
                if not (name.startswith(base + ".") and name.endswith(".bak") and os.path.isfile(b)):
                    continue
                if b == orig:
                    os.makedirs(d, exist_ok=True)
                    shutil.move(b, os.path.join(d, name))
                    orig = os.path.join(d, name)
                    self._write_private(self.paths.backup_map,
                                        store.backup_move(store.read_text(self.paths.backup_map), path, orig))
                else:
                    os.remove(b)
        if not os.path.isdir(d):
            return
        baks = sorted(os.path.join(d, n) for n in os.listdir(d) if n.startswith(base + ".") and n.endswith(".bak"))
        kept = 0
        for b in reversed(baks):   # names end in a timestamp: newest last
            if b == orig:
                continue
            kept += 1
            if kept > KEEP_CHANGES:
                os.remove(b)

    def forget(self, path: str) -> None:
        """Delete a file these tools created, its change copies and its backup
        record (the copies may hold keys; rollback has nothing to restore)."""
        d = self.change_dir(path)
        targets = [path]
        if os.path.isdir(d):
            base = os.path.basename(path)
            targets += [os.path.join(d, n) for n in os.listdir(d) if n.startswith(base + ".") and n.endswith(".bak")]
        self.runner.run(["rm", "-f"] + targets)
        if not self.runner.dry_run:
            rec = store.read_text(self.paths.backup_map)
            if rec is not None:
                self._write_private(self.paths.backup_map, store.backup_forget(rec, path))

    def restore_original(self, path: str, remove_new: bool) -> None:
        """Restore a file's original copy; files we created are removed if <remove_new>."""
        orig = store.backup_orig(store.read_text(self.paths.backup_map), path)
        if not orig:
            return
        if orig == "NEW":
            if remove_new:
                self.runner.run(["rm", "-f", path])
            elif os.path.exists(path):
                self.out.info(f"kept {path} (created by wireguard-tools; --purge deletes it)")
        elif os.path.exists(orig):
            self.runner.run(["cp", "-a", orig, path])
            self.out.info(f"restored {path} from {orig}")
        else:
            self.out.warn(f"original backup {orig} is missing; left {path} as is")

    def _write_private(self, path: str, content: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
