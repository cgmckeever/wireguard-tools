"""wgt-ctl's foundations: where things are here and on a server, the server list
(local/servers), and reaching a server over ssh."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .. import store, ui
from ..const import Paths
from ..errors import WgtError
from ..system import Out

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


LOCAL = os.path.join(ROOT, "local")


LIST = os.path.join(LOCAL, "servers")


DEVICES = os.path.join(LOCAL, "devices")               # each device's static number (fleet.py)


LINKS = os.path.join(LOCAL, "exits", "details.tsv")   # server, local file, code, full name (tab-separated)


DEF_DIR = "/opt/wireguard-tools"
DEF_WGT = f"{DEF_DIR}/bin/wgt"


REMOTE = Paths()                                       # where things are on a server


STATE_FILE = REMOTE.state


INBOX = os.path.join(REMOTE.exit_dir, "incoming")


_SSH_BASE = ["-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15"]


# Host keys must already be known (whatever ~/.ssh/config says); only the first
# login to a new machine asks, so you can check the key against your provider's console.
SSH_OPTS = _SSH_BASE + ["-o", "StrictHostKeyChecking=yes"]


SSH_FIRST_LOGIN_OPTS = _SSH_BASE + ["-o", "StrictHostKeyChecking=ask", "-o", "BatchMode=no"]


RESERVED = ("init", "clients", "devices", "summary", "all", "help", "list")


SERVER_NAME_RULE = f"a letter first, then up to 31 of A-Za-z0-9_- (not {', '.join(RESERVED)} or a command's name)"


# wgt-ctl's own per-server commands.
CTL_COMMANDS = ("pull", "backup", "jump-add", "ssh", "branch", "forget", "remove", "move")
# The wgt commands (anything else after a server word is a mistake, not a command).
WGT_COMMANDS = ("setup", "summary", "status", "test", "log", "backup", "restore", "rollback", "client",
                "device", "jump", "exit", "tunnel", "warp")


TARGET = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.:\[\]-]+$")


CHECKOUT_DIR = re.compile(r"^/[A-Za-z0-9._/-]+$")


BRANCH = re.compile(r"^[A-Za-z0-9._/][A-Za-z0-9._/-]*$")


out = Out()


@dataclass
class Server:
    """One line of local/servers."""

    name: str
    target: str
    dir: str = DEF_DIR

    @property
    def wgt(self) -> str:
        return shlex.quote(f"{self.dir}/bin/wgt")


def ensure_local() -> None:
    """local/ and its private dirs (700), and an empty servers list (600)."""
    for d in (LOCAL, os.path.join(LOCAL, "exits"), os.path.join(LOCAL, "backups")):
        private_dir(d)
    if not os.path.exists(LIST):
        with open(LIST, "w") as f:
            f.write("# Servers this machine manages (untracked): name  ssh-target  [checkout]\n")
    os.chmod(LIST, 0o600)


def servers() -> List[Server]:
    """local/servers, checked: its names become local paths and its targets and
    directories go into ssh and remote commands."""
    found: List[Server] = []
    for n, line in enumerate((store.read_text(LIST) or "").splitlines(), 1):
        fields = line.split()
        if not fields or fields[0].startswith("#"):
            continue
        name, target = fields[0], fields[1] if len(fields) > 1 else ""
        checkout = fields[2] if len(fields) > 2 else DEF_DIR
        if len(fields) < 2 or not valid_server(name) or not TARGET.match(target) or not CHECKOUT_DIR.match(checkout):
            raise WgtError(f"{LIST} line {n}: want 'name user@host [/checkout/dir]' "
                           f"(name: {SERVER_NAME_RULE}): {line.strip()}")
        found.append(Server(name, target, checkout))
    return found


def find(sel: str) -> Server:
    """A server by name or list number."""
    for i, s in enumerate(servers(), 1):
        if sel in (s.name, str(i)):
            return s
    raise WgtError(f"no server '{sel}' in {LIST}")


def pick() -> Server:
    """Ask which server, from the numbered list."""
    if not servers():
        raise WgtError("no servers yet (add one: wgt-ctl init)")
    if not ui.have_tty():
        raise WgtError("no server given (and no terminal to pick one)")
    cmd_list(quick=True)
    reply = ui.ask("  Which server? [number or name] ")
    if not reply:
        raise WgtError("no server chosen")
    return find(reply)


def exits_dir(server: str) -> str:
    """Where <server>'s jump configs wait to be pushed."""
    return os.path.join(LOCAL, "exits", server)


def staged(server: str) -> List[str]:
    """<server>'s staged jump configs (local/exits/<server>/*.conf)."""
    d = exits_dir(server)
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".conf")) if os.path.isdir(d) else []


def valid_server(name: str) -> bool:
    """A letter first (so it can't be mistaken for a list number), not a reserved word."""
    return (bool(re.match(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$", name))
            and name not in RESERVED and name not in CTL_COMMANDS and name not in WGT_COMMANDS)


def private_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def ssh(target: str, command: str, tty: bool = False, batch: bool = False, capture: bool = False,
        input: Optional[str] = None) -> Tuple[int, str]:
    """Run <command> on <target>, under bash whatever root's login shell is
    (a zsh login shell trips over unmatched globs). tty: with a terminal when
    we have one (prompts, QR codes); batch: never prompt, never read stdin (probes)."""
    command = "bash -c " + shlex.quote(command)
    args = ["ssh"] + SSH_OPTS
    if batch:
        args += ["-n", "-o", "BatchMode=yes"]
    elif tty and sys.stdin.isatty() and sys.stdout.isatty():
        args += ["-t", "-q"]
    args += [target, command]
    p = subprocess.run(args, text=True, input=input,
                       stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.DEVNULL if batch else None,
                       stdin=subprocess.DEVNULL if batch else None)
    if p.returncode == 255 and not batch:
        out.error(f"ssh to {target} failed (unreachable, or its host key isn't known/doesn't match)")
    return p.returncode, p.stdout or ""


def can_login(target: str) -> bool:
    return ssh(target, "true", batch=True, capture=True)[0] == 0


def scp(src: str, dest: str) -> bool:
    return subprocess.run(["scp", "-q", "-p"] + SSH_OPTS + [src, dest]).returncode == 0


def q(*args: str) -> str:
    return " ".join(shlex.quote(a) for a in args)


def cmd_list(quick: bool = False) -> None:
    """The numbered server list; quick skips asking each server for its branch."""
    ss = servers()
    if not ss:
        out.info("no servers yet (add one: wgt-ctl init)")
        return
    print("")
    for i, s in enumerate(ss, 1):
        jumps = ",".join(sorted(os.path.basename(f)[:-len(".conf")] for f in staged(s.name)))
        probe = ""
        if not quick:
            dq = shlex.quote(s.dir)
            rc, o = ssh(s.target, f"cd {dq} && b=$(git rev-parse --abbrev-ref HEAD) && c=$(git rev-parse --short HEAD) "
                                  "&& printf '%s@%s' \"$b\" \"$c\"", batch=True, capture=True)
            probe = o.strip() if rc == 0 else "unreachable"
        note = f"jumps staged: {jumps}" if jumps else ""
        print(f"  {i:>2}) {s.name:<10} {s.target:<24} {probe:<22} {note}".rstrip())
    print("")


def repo_url() -> str:
    """This checkout's origin, as an https URL (servers clone anonymously)."""
    p = subprocess.run(["git", "-C", ROOT, "remote", "get-url", "origin"], stdout=subprocess.PIPE, text=True)
    url = p.stdout.strip()
    if p.returncode != 0 or not url:
        raise WgtError(f"no git remote 'origin' in {ROOT}")
    m = re.match(r"^git@([^:]+):(.+)$", url)   # servers clone anonymously
    return f"https://{m.group(1)}/{m.group(2)}" if m else url


def do_wgt(s: Server, args: List[str]) -> int:
    """Any wgt command, run there. Printed scp hints use this machine's login."""
    return ssh(s.target, f"WGT_SSH_TARGET={shlex.quote(s.target)} {s.wgt} {q(*args)}", tty=True)[0]


def no_args(cmd: str, args: List[str]) -> None:
    if args:
        raise WgtError(f"wgt-ctl <server> {cmd} takes no arguments (got: {' '.join(args)})")


YES = ("--yes", "-y")


def only_yes(cmd: str, args: List[str]) -> bool:
    """For commands whose only option is --yes: refuse anything else; True if given."""
    if set(args) - set(YES):
        raise WgtError(f"usage: wgt-ctl <server> {cmd} [--yes]")
    return any(a in YES for a in args)


def parse_opts(cmd: str, args: List[str], opts: Dict[str, str], help_text: str,
               flags: Sequence[str] = ()) -> Tuple[Dict[str, str], List[str], bool]:
    """A command's options: <opts> (option -> default) with what was given, what follows
    `--` (passed on), and whether any of <flags> came."""
    flagged = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return opts, args[i + 1:], flagged
        if a in flags:
            flagged = True
            i += 1
            continue
        if a not in opts:
            raise WgtError(f"unknown option: {a} ({help_text})")
        if i + 1 >= len(args):
            raise WgtError(f"{cmd}: {a} needs a value")
        opts[a] = args[i + 1]
        i += 2
    return opts, [], flagged


def needs_terminal(cmd: str) -> None:
    """A command about to ask, without --yes: there must be a terminal to ask on."""
    if not ui.have_tty():
        raise WgtError(f"{cmd} needs --yes without a terminal")


def write_local(path: str, text: str, mode: int = 0o600) -> None:
    """A file under local/, whole or not at all: the server list and numbers aren't
    recoverable from anywhere else."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def set_target(name: str, target: str) -> None:
    """Point a server's line in local/servers at a new login (its checkout dir stays)."""
    lines = []
    for line in (store.read_text(LIST) or "").splitlines():
        f = line.split()
        if f and f[0] == name:
            line = f"{name:<10} {target}" + (f" {f[2]}" if len(f) > 2 else "")
        lines.append(line)
    write_local(LIST, "\n".join(lines) + "\n")
