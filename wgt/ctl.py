"""`wgt-ctl`: drives the servers listed in local/servers from your own machine
(macOS or Linux). Most commands are  wgt-ctl SERVER COMMAND  where SERVER is
a name, a list number, or all; a few take no server (below).

One server:
  wgt-ctl SERVER <wgt command>          run it there, e.g. wgt-ctl boba client add phone --exit par-fr
  wgt-ctl SERVER pull | backup          git pull (fast-forward) / back up, fetched to local/backups/SERVER/
  wgt-ctl SERVER jump-add [n|all]       push a WireGuard config from local/exits/SERVER/ as a jump (old: exit-add)
  wgt-ctl SERVER ssh [command] | branch <b>
  wgt-ctl SERVER forget [--yes]         drop it from local/servers (the server is untouched)
  wgt-ctl SERVER remove [--yes]         tear wireguard-tools off it (rollback --purge, delete its
                                        checkout, wgcf and archives), then forget it

Every server, in turn:
  wgt-ctl all <wgt command> | pull | backup | branch <b>

No server:
  wgt-ctl [list]                        the servers: branch@commit (or unreachable), jumps staged
  wgt-ctl summary                       every server at a glance (wgt summary on each)
  wgt-ctl clients                       every client on every server
  wgt-ctl init [--address A] [--name N] [--user U] [--branch B] [-- setup flags]
                                        set up a new server (or adopt one already running these tools)

Leave SERVER out of a server command to pick one from the list. Private
files stay under local/ (untracked): servers, exits/<server>/*.conf (jump
WireGuard configs, any name), exits/details.tsv (which file is which jump), backups/."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import render, store, ui
from .ctx import VALUE_FLAGS
from .errors import WgtError
from .system import Out

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL = os.path.join(ROOT, "local")
LIST = os.path.join(LOCAL, "servers")
LINKS = os.path.join(LOCAL, "exits", "details.tsv")   # server, local file, code, full name (tab-separated)
DEF_DIR = "/opt/wireguard-tools"
INBOX = "/etc/wireguard/exits/incoming"
# Host keys must already be known (whatever ~/.ssh/config says); only init's
# first login asks, so you can check the key against your provider's console.
SSH_OPTS = ["-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "StrictHostKeyChecking=yes"]
RESERVED = ("init", "clients", "summary", "all", "help", "list")
SERVER_NAME_RULE = f"a letter first, then up to 31 of A-Za-z0-9_- (not {', '.join(RESERVED)})"
# The wgt commands (anything else after a server word is a mistake, not a command).
WGT_COMMANDS = ("setup", "warp", "summary", "status", "test", "log", "backup", "restore", "rollback", "client",
                "jump", "exit")
_TARGET = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.:\[\]-]+$")
_DIR = re.compile(r"^/[A-Za-z0-9._/-]+$")
_BRANCH = re.compile(r"^[A-Za-z0-9._/][A-Za-z0-9._/-]*$")

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


# --- the server list ---

def ensure_local() -> None:
    """local/ and its private dirs (700), and an empty servers list (600)."""
    for d in (LOCAL, os.path.join(LOCAL, "exits"), os.path.join(LOCAL, "backups")):
        private_dir(d)
    if not os.path.exists(LIST):
        with open(LIST, "w") as f:
            f.write("# Servers this machine manages (untracked): name  ssh-target  [checkout]\n")
    os.chmod(LIST, 0o600)


NO_WGT = 3   # the remote exit status when a server has no bin/wgt


def no_wgt(s: Server) -> str:
    """Why a server can't run wgt commands: its checkout predates wgt (or is missing)."""
    return f"no {s.dir}/bin/wgt there; update it: wgt-ctl {s.name} pull"


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
        if len(fields) < 2 or not valid_server(name) or not _TARGET.match(target) or not _DIR.match(checkout):
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


def valid_server(name: str) -> bool:
    """A letter first (so it can't be mistaken for a list number), not a reserved word."""
    return bool(re.match(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$", name)) and name not in RESERVED


def private_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    os.chmod(path, 0o700)
    return path


# --- ssh ---

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


# --- commands ---

def cmd_list(quick: bool = False) -> None:
    """The numbered server list; quick skips asking each server for its branch."""
    ss = servers()
    if not ss:
        out.info("no servers yet (add one: wgt-ctl init)")
        return
    print("")
    for i, s in enumerate(ss, 1):
        d = os.path.join(LOCAL, "exits", s.name)
        staged = ",".join(sorted(f[:-5] for f in os.listdir(d) if f.endswith(".conf"))) if os.path.isdir(d) else ""
        probe = ""
        if not quick:
            dq = shlex.quote(s.dir)
            rc, o = ssh(s.target, f"cd {dq} && b=$(git rev-parse --abbrev-ref HEAD) && c=$(git rev-parse --short HEAD) "
                                  "&& printf '%s@%s' \"$b\" \"$c\"", batch=True, capture=True)
            probe = o.strip() if rc == 0 else "unreachable"
        note = f"jumps staged: {staged}" if staged else ""
        print(f"  {i:>2}) {s.name:<10} {s.target:<24} {probe:<22} {note}".rstrip())
    print("")


def cmd_init(argv: List[str]) -> None:
    """Set up a new server (or adopt one already running these tools) and add it to the list."""
    opts, setup_args = _parse_init(argv)
    ensure_local()
    address, name, target = _ask_identity(opts)
    out.log(f"Connecting to {target}")
    _login(target)
    facts = _probe(target, address)
    saved = facts.get("saved", "")
    if saved or facts.get("checkout") == "1":
        _adopt(name, saved, target)
        return
    _install(target, opts["--branch"])
    out.log(f"Running wgt setup --name {name} on {address}")
    rc, _ = ssh(target, f"{DEF_DIR}/bin/wgt setup --name {q(name)} {q(*setup_args)}", tty=True)
    if rc != 0:
        raise WgtError(f"setup did not finish; fix the cause and re-run: wgt-ctl init --address {address} "
                       f"--name {name} --user {opts['--user']} --branch {opts['--branch']}")
    record(name, target)
    out.info(f"next: wgt-ctl {name} client add <client>   (or add jumps: wgt-ctl {name} jump-add)")


def _parse_init(argv: List[str]) -> Tuple[Dict[str, str], List[str]]:
    """init's options, and what follows `--` (passed on to wgt setup)."""
    opts = {"--address": "", "--name": "", "--user": "root", "--branch": "main"}
    i = 0
    while i < len(argv):
        if argv[i] == "--":
            return opts, argv[i + 1:]
        if argv[i] not in opts:
            raise WgtError(f"unknown option: {argv[i]} (see: wgt-ctl help)")
        if i + 1 >= len(argv):
            raise WgtError(f"init: {argv[i]} needs a value")
        opts[argv[i]] = argv[i + 1]
        i += 2
    return opts, []


def _ask_identity(opts: Dict[str, str]) -> Tuple[str, str, str]:
    """(address, name, ssh target) from the options or asked, all checked."""
    address, name = opts["--address"], opts["--name"]
    if (not address or not name) and not ui.have_tty():
        raise WgtError("init needs --address and --name without a terminal")
    address = address or ui.ask("    Server address (IP or hostname): ")
    if not address:
        raise WgtError("no address")
    name = name or ui.ask("    Server name (e.g. boba): ")
    if not valid_server(name):
        raise WgtError(f"server name must be {SERVER_NAME_RULE}: '{name}'")
    if any(s.name == name for s in servers()):
        raise WgtError(f"'{name}' is already in {LIST}")
    if not _BRANCH.match(opts["--branch"]):
        raise WgtError(f"bad branch name: {opts['--branch']}")
    target = f"{opts['--user']}@{address}"
    if not _TARGET.match(target):
        raise WgtError(f"bad --user or --address: {target}")
    return address, name, target


def _login(target: str) -> None:
    """Make sure ssh works without questions: accept the host key once (the
    user compares the fingerprint), and offer to install their SSH key."""
    if can_login(target):
        return
    out.info("If ssh asks about the host key, compare the fingerprint with your provider's")
    out.info("console before answering yes.")
    first = [o for o in SSH_OPTS if o != "StrictHostKeyChecking=yes"] + ["-o", "StrictHostKeyChecking=ask"]
    if subprocess.run(["ssh", *first, "-o", "BatchMode=no", target, "true"]).returncode != 0:
        raise WgtError(f"cannot log in to {target}")
    if can_login(target) or not ui.yes("Log in with your SSH key from now on (ssh-copy-id)?", default=True):
        return
    if subprocess.run(["ssh-copy-id", target]).returncode != 0:
        raise WgtError("ssh-copy-id failed")


def _probe(target: str, address: str) -> Dict[str, str]:
    """What init needs to know about the server: uid, OS, an existing checkout,
    a saved server name. Refuses non-root logins and unsupported systems."""
    rc, text = ssh(target, f"""echo "uid=$(id -u)"
        . /etc/os-release 2>/dev/null && echo "os=$ID $VERSION_ID"
        [ -d {DEF_DIR}/.git ] && echo checkout=1
        sed -n "s/^SERVER_NAME=/saved=/p" /etc/wireguard/wg-tools.state 2>/dev/null | tail -n 1""", capture=True)
    if rc != 0:
        raise WgtError(f"cannot reach {target}")
    facts = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    if facts.get("uid") != "0":
        raise WgtError("log in as root (wgt needs it): --user root")
    osname = facts.get("os", "")
    out.info(f"{address}: {osname or 'unknown OS'}")
    if not re.match(r"^(debian|ubuntu) ", osname):
        raise WgtError(f"unsupported OS '{osname}' (Debian 11+ or Ubuntu 20.04+)")
    return facts


def _adopt(name: str, saved: str, target: str) -> None:
    """The server already runs these tools (at any version): record
    it under its saved name; never re-install."""
    if not saved:
        raise WgtError(f"{DEF_DIR} exists there but isn't set up; set it up there, or remove it and re-run init")
    if not valid_server(saved):
        raise WgtError(f"the server's saved name '{saved}' isn't a valid name")
    out.log(f"wireguard-tools is already set up there as '{saved}'")
    if saved != name:
        out.info(f"recording it as '{saved}' (its saved name); rename it with: wgt-ctl {saved} setup --name {name}")
        if any(s.name == saved for s in servers()):
            raise WgtError(f"'{saved}' is already in {LIST}")
    record(saved, target)
    out.info(f"update it to this version with: wgt-ctl {saved} pull")


def _install(target: str, branch: str) -> None:
    """git (if missing) and a checkout of <branch> at DEF_DIR."""
    url = repo_url()
    out.log(f"Installing git and cloning {url} ({branch}) to {DEF_DIR}")
    rc, _ = ssh(target, f"""set -e
        export DEBIAN_FRONTEND=noninteractive
        command -v git >/dev/null || {{ apt-get update -qq && apt-get install -y -qq git >/dev/null; }}
        if [ -d {DEF_DIR}/.git ]; then
            git -C {DEF_DIR} fetch -q && git -C {DEF_DIR} checkout -q {q(branch)} && git -C {DEF_DIR} pull --ff-only -q
        else git clone -q -b {q(branch)} {q(url)} {DEF_DIR}; fi
        git -C {DEF_DIR} log --oneline -1""")
    if rc != 0:
        raise WgtError("clone failed")


def record(name: str, target: str) -> None:
    """Add a server to local/servers, with its exits dir."""
    ensure_local()
    with open(LIST, "a") as f:
        f.write(f"{name:<10} {target}\n")
    private_dir(os.path.join(LOCAL, "exits", name))
    out.log(f"Added '{name}' ({target}) to {LIST}")


def repo_url() -> str:
    """This checkout's origin, as an https URL (servers clone anonymously)."""
    p = subprocess.run(["git", "-C", ROOT, "remote", "get-url", "origin"], stdout=subprocess.PIPE, text=True)
    url = p.stdout.strip()
    if p.returncode != 0 or not url:
        raise WgtError(f"no git remote 'origin' in {ROOT}")
    m = re.match(r"^git@([^:]+):(.+)$", url)   # servers clone anonymously
    return f"https://{m.group(1)}/{m.group(2)}" if m else url


def cmd_summary() -> int:
    """`wgt summary` from every server, one block each, fetched in parallel
    and shown in list order. 1 if any server couldn't answer."""
    from concurrent.futures import ThreadPoolExecutor

    def fetch(s: Server) -> Tuple[int, str]:
        return ssh(s.target, f"if [ -x {s.wgt} ]; then {s.wgt} summary 2>&1; else exit {NO_WGT}; fi",
                   batch=True, capture=True)

    todo = servers()
    if not todo:
        raise WgtError("no servers yet (add one: wgt-ctl init)")
    with ThreadPoolExecutor(max_workers=len(todo)) as pool:
        answers = list(pool.map(fetch, todo))
    rc = 0
    for s, (code, text) in zip(todo, answers):
        print("")
        if code == 0 and text.strip():
            print(text.rstrip("\n"))
            continue
        rc = 1
        last = text.strip().splitlines()[-1] if text.strip() else f"failed (exit {code})"
        why = {NO_WGT: no_wgt(s), 255: "unreachable"}.get(code, last)
        print(f"{s.name}  ({s.target})")
        print(f"    {why}")
    print("")
    return rc


def cmd_clients() -> None:
    """Every client on every server in one table."""
    fmt = "  {:<10} " + ui.CLIENT_FMT
    print("")
    print(fmt.format("SERVER", *ui.CLIENT_HEAD))
    for s in servers():
        rc, o = ssh(s.target, f"if [ -x {s.wgt} ]; then {s.wgt} client list --tsv; else exit {NO_WGT}; fi",
                    batch=True, capture=True)
        if rc != 0:
            note = {NO_WGT: no_wgt(s), 255: "unreachable"}.get(rc, f"client list failed: exit {rc}")
            print(f"  {s.name:<10} ({note})")
            continue
        rows = sorted((ui.ClientRow.from_tsv(line) for line in o.splitlines() if line),
                      key=lambda row: store.device_key(row.name))
        if not rows:
            print(f"  {s.name:<10} (no clients)")
        for row in rows:
            print(fmt.format(s.name, *row.cells()))
    print("")


# --- per-server commands ---

def do_wgt(s: Server, args: List[str]) -> int:
    """Any wgt command, run there. Printed scp hints use this machine's login."""
    env = f"WGT_SSH_TARGET={shlex.quote(s.target)}"
    missing = f"echo {shlex.quote('ERROR: ' + no_wgt(s))} >&2; exit 1"
    return ssh(s.target, f"if [ -x {s.wgt} ]; then {env} {s.wgt} {q(*args)}; else {missing}; fi", tty=True)[0]


def no_args(cmd: str, args: List[str]) -> None:
    if args:
        raise WgtError(f"wgt-ctl <server> {cmd} takes no arguments (got: {' '.join(args)})")


def do_pull(s: Server, args: List[str]) -> int:
    """git pull its checkout (fast-forward only). A pull changes no running
    config; when rendering changed (tools/render-diff.sh shows it), run setup."""
    no_args("pull", args)
    d = shlex.quote(s.dir)
    # --ff-only: a checkout with local commits or edits stops with git's message
    # instead of merging on a production server.
    return ssh(s.target, f"git -C {d} pull --ff-only -q && git -C {d} log --oneline -1 | sed 's/^/    /'")[0]


def do_branch(s: Server, args: List[str]) -> int:
    """Switch its checkout to <branch>, then pull."""
    if len(args) != 1 or not _BRANCH.match(args[0]):
        raise WgtError("usage: wgt-ctl <server> branch <branch>")
    d = shlex.quote(s.dir)
    return ssh(s.target, f"set -e; git -C {d} fetch -q; git -C {d} checkout -q {q(args[0])}; "
                         f"git -C {d} pull --ff-only -q; "
                         f"printf '    %s: ' \"$(git -C {d} rev-parse --abbrev-ref HEAD)\"; "
                         f"git -C {d} log --oneline -1")[0]


def do_ssh(s: Server, args: List[str]) -> int:
    """Log in (or run <args> there as a command)."""
    return subprocess.run(["ssh"] + SSH_OPTS + [s.target] + ([q(*args)] if args else [])).returncode


def do_backup(s: Server, args: List[str]) -> int:
    """wgt backup there, then fetch the archive into local/backups/<server>/."""
    no_args("backup", args)
    rc, o = ssh(s.target, f"{s.wgt} backup --yes >/dev/null || exit 1; "
                          "ls -t /var/backups/wireguard-tools/wireguard-tools.*.tar.gz 2>/dev/null | head -n 1",
                capture=True)
    archive = o.strip()
    if rc != 0 or not archive:
        out.error(f"{s.name}: backup failed")
        return 1
    ensure_local()
    d = private_dir(os.path.join(LOCAL, "backups", s.name))
    if not scp(f"{s.target}:{archive}", d + "/"):
        out.error(f"{s.name}: fetching {archive} failed")
        return 1
    local = os.path.join(d, os.path.basename(archive))
    os.chmod(local, 0o600)
    out.info(f"{s.name}: {local} (private keys inside; restore with wgt restore)")
    return 0


YES = ("--yes", "-y")


def only_yes(cmd: str, args: List[str]) -> bool:
    """For commands whose only option is --yes: refuse anything else; True if given."""
    if set(args) - set(YES):
        raise WgtError(f"usage: wgt-ctl <server> {cmd} [--yes]")
    return any(a in YES for a in args)


def needs_terminal(cmd: str) -> None:
    """A command about to ask, without --yes: there must be a terminal to ask on."""
    if not ui.have_tty():
        raise WgtError(f"{cmd} needs --yes without a terminal")


def do_remove(s: Server, args: List[str]) -> int:
    """Tear wireguard-tools off a server: rollback --purge (keys, clients,
    WARP accounts, jumps, rules, units; original files restored), then
    its checkout, the wgt link, wgcf and the backup archives. Packages stay.
    Only after all that succeeds is it forgotten here. Asks for the server's
    name unless --yes."""
    sure = only_yes("remove", args)
    if s.dir.rstrip("/").count("/") < 2:
        raise WgtError(f"refusing to delete checkout '{s.dir}' (too close to /)")
    out.warn(f"this removes wireguard-tools from {s.name} ({s.target}): its keys, clients, WARP accounts, "
             f"jumps and backup archives, and {s.dir}. Its clients stop working. Irreversible "
             f"(want a copy first? wgt-ctl {s.name} backup)")
    if not sure:
        needs_terminal("remove")
        if ui.ask(f"    Type the server's name ({s.name}) to remove it: ") != s.name:
            out.info("kept")
            return 0
    d = shlex.quote(s.dir)
    # set -e: if rollback fails, nothing is deleted and the server stays listed.
    rc, _ = ssh(s.target, f"""set -e
        if [ -x {d}/bin/wgt ]; then {d}/bin/wgt rollback --purge --yes; fi
        rm -rf {d} /var/backups/wireguard-tools
        rm -f /usr/local/bin/wgcf /usr/local/sbin/wgt
        echo "    removed {s.dir}, /usr/local/sbin/wgt, /usr/local/bin/wgcf and /var/backups/wireguard-tools"
        echo "    (packages left installed)"
        """, tty=True)
    if rc != 0:
        raise WgtError(f"teardown failed on {s.name} (exit {rc}); it stays in {LIST} - fix the cause and re-run")
    return do_forget(s, ["--yes"])


def do_forget(s: Server, args: List[str]) -> int:
    """Drop it from local/servers; nothing on the server changes."""
    if not only_yes("forget", args):
        needs_terminal("forget")
        if not ui.yes(f"Remove '{s.name}' from {LIST}? (nothing on the server changes)", default=False):
            out.info("kept")
            return 0
    lines = [line for line in (store.read_text(LIST) or "").splitlines() if (line.split() or [""])[0] != s.name]
    with open(LIST, "w") as f:
        f.write("\n".join(lines) + "\n")
    out.info(f"forgot '{s.name}'; its local/exits/{s.name} and local/backups/{s.name} stay")
    return 0


# --- jumps from local files ---

def links() -> List[Tuple[str, str, str, str]]:
    """local/exits/details.tsv: (server, local file, code, full name) per named config."""
    rows = [line.split("\t") for line in (store.read_text(LINKS) or "").splitlines()]
    return [(f[0], f[1], f[2], f[3]) for f in rows if len(f) >= 4]


def link_of(server: str, file: str) -> Optional[Tuple[str, str]]:
    """(code, full name) for a local exit config, if it has been named."""
    return next(((code, label) for srv, f, code, label in links() if srv == server and f == file), None)


def make_link(server: str, path: str) -> Tuple[str, str]:
    """First push of a local config: ask what it is and pick its code (the name's
    first 3 letters + a country tag like Proton's "FR#252", e.g. par-fr). A code another of
    this server's files already uses is refused."""
    file = os.path.basename(path)
    hint = render.peer_hint(store.read_text(path) or "")
    if not ui.have_tty():
        raise WgtError(f"{file} isn't named yet; run jump-add in a terminal once to name it")
    name = ui.ask(f"    What is {file} ({hint or 'no hint'})? e.g. Paris, France: ")
    if not name:
        raise WgtError(f"no name for {file}")
    m = re.match(r"^([A-Za-z]{2})#", hint)
    city = re.sub(r"[^a-z]", "", name.lower())[:3]
    code = city + (f"-{m.group(1).lower()}" if m else "")
    taken = {code for srv, _file, code, _label in links() if srv == server}
    while True:
        reply = ui.ask(f"    Short code on the server [{code}]: ")
        if not reply and not store.valid_exit_code(code):
            raise WgtError("no code given")
        code = reply or code
        if not store.valid_exit_code(code):
            out.info(f"the code must be {store.EXIT_CODE_RULE}")
        elif code in taken:
            out.info(f"{server} already has a jump '{code}'; pick another")
        else:
            break
    with open(LINKS, "a") as f:
        f.write(f"{server}\t{file}\t{code}\t{name}\n")
    os.chmod(LINKS, 0o600)
    return code, name


def with_local_file_comment(text: str, file: str) -> str:
    """The config with a "# local file: <name>" line naming where it came from
    (any earlier one replaced)."""
    body = "".join(line + "\n" for line in text.splitlines() if not line.startswith("# local file: "))
    return f"# local file: {file}\n" + body


def add_one_exit(s: Server, path: str) -> None:
    """Copy one local config to the server and add it there as its jump;
    the uploaded copy is deleted once imported."""
    file = os.path.basename(path)
    os.chmod(path, 0o600)
    link = link_of(s.name, file) or make_link(s.name, path)
    code, label = link
    if not store.valid_exit_code(code):
        raise WgtError(f"bad code '{code}' for {file} in {LINKS}")
    out.log(f"{s.name}: {file} -> jump {code} ({label})")
    if ssh(s.target, f"install -d -m 700 /etc/wireguard/exits {INBOX}")[0] != 0:
        raise WgtError(f"cannot reach {s.target}")
    fd, tmp = tempfile.mkstemp()
    try:
        with os.fdopen(fd, "w") as f:
            f.write(with_local_file_comment(store.read_text(path) or "", file))
        os.chmod(tmp, 0o600)
        if not scp(tmp, f"{s.target}:{INBOX}/{code}.conf"):
            raise WgtError("upload failed")
    finally:
        os.remove(tmp)
    # `exit add`, not `jump add`: servers still on older code know only that name.
    rc, _ = ssh(s.target, f"{s.wgt} exit add {q(code, f'{INBOX}/{code}.conf', '--label', label, '--yes')}; "
                          f"rc=$?; rm -f {INBOX}/{code}.conf; exit $rc")
    if rc != 0:
        raise WgtError(f"jump add failed for {code}")


def local_exit_files(s: Server) -> List[str]:
    """This server's staged jump configs (local/exits/<server>/*.conf)."""
    src = os.path.join(LOCAL, "exits", s.name)
    return sorted(os.path.join(src, f) for f in os.listdir(src) if f.endswith(".conf")) if os.path.isdir(src) else []


def remote_exit_codes(s: Server) -> List[str]:
    """The jump codes the server has (read from its exits.db directly, so
    it also works on a server that hasn't pulled a newer wgt yet)."""
    _, o = ssh(s.target, "awk '!/^#/ { print $1 }' /etc/wireguard/exits.db 2>/dev/null", batch=True, capture=True)
    return o.split()


def print_exit_files(s: Server, files: List[str], remote: List[str]) -> None:
    """The staged configs, numbered: file, code and name (once named), on the server or not."""
    w = max(4, *(len(os.path.basename(f)) for f in files))
    print("")
    print(f"  {'#':>3}  {'FILE':<{w}}  {'CODE':<7} {'NAME':<20} ON SERVER")
    for i, f in enumerate(files, 1):
        link = link_of(s.name, os.path.basename(f))
        code, label = link if link else ("-", "(not named yet)")
        print(f"  {i:>3}  {os.path.basename(f):<{w}}  {code:<7} {label:<20} {'yes' if code in remote else 'no'}")
    print("")


def client_name(args: List[str]) -> str:
    """The client name in `client add ...` words: the first word that is
    neither a flag nor a value flag's value (`--warp off par-fr.mac`)."""
    words = iter(args[1:])
    for a in words:
        if a in VALUE_FLAGS:
            next(words, None)
        elif not a.startswith("-"):
            return a
    return ""


def offer_staged_exits(s: Server, args: List[str]) -> None:
    """Before `client add` in a terminal: if exit configs are staged here but
    not on the server, list them and offer to push one, so the server's exit
    menu can offer it. Skipped when the exit is already given."""
    name = client_name(args)
    # Nothing to offer when the exit is given (--exit, or a <code>.<name>
    # client name) or there must be no prompts (--yes).
    if "--exit" in args or any(a.startswith("--exit=") for a in args) or "." in name or any(a in YES for a in args):
        return
    files = local_exit_files(s)
    if not files or not ui.have_tty():
        return
    remote = remote_exit_codes(s)
    staged = [f for f in files if (link_of(s.name, os.path.basename(f)) or ("", ""))[0] not in remote]
    if not staged:
        return
    out.log(f"Jump configs staged here but not on {s.name}")
    print_exit_files(s, staged, remote)
    sel = ui.ask("    Push one first, so the new client can use it? [number, Enter skips]: ")
    if not sel:
        return
    if not sel.isdigit() or not 1 <= int(sel) <= len(staged):
        raise WgtError(f"no staged config {sel}")
    add_one_exit(s, staged[int(sel) - 1])


def do_exit_add(s: Server, args: List[str]) -> int:
    """Push one of this server's local configs (by its number in the list) or all."""
    src = os.path.join(LOCAL, "exits", s.name)
    files = local_exit_files(s)
    if not files:
        raise WgtError(f"no configs in {src} (save WireGuard configs there, any name ending .conf)")
    os.chmod(src, 0o700)
    sel = args[0] if args else ""
    if not sel:
        if not ui.have_tty():
            raise WgtError(f"no config given (and no terminal to pick one): wgt-ctl {s.name} jump-add <n|all>")
        print_exit_files(s, files, remote_exit_codes(s))
        sel = ui.ask("  Add which? [number or all]: ")
    if not sel:
        raise WgtError("nothing chosen")
    if sel == "all":
        picked = files
    elif sel.isdigit() and 1 <= int(sel) <= len(files):
        picked = [files[int(sel) - 1]]
    else:
        raise WgtError(f"no config number '{sel}' (1-{len(files)}, or all)")
    for f in picked:
        add_one_exit(s, f)
    out.log(f"{s.name} jumps")
    return do_wgt(s, ["exit", "list"])   # the old name: works on servers not yet pulled


CTL = {"pull": do_pull, "backup": do_backup, "jump-add": do_exit_add, "exit-add": do_exit_add, "ssh": do_ssh,
       "branch": do_branch, "forget": do_forget, "remove": do_remove}
ALL_OK = ("pull", "backup", "branch")   # ctl commands that take "all" (every wgt command does)


def on(sel: str, cmd: str, args: List[str]) -> int:
    """Run a command on one server, or (with "all") on each in turn: one failing
    doesn't stop the rest; the result is 1 if any failed."""
    fn = CTL.get(cmd)
    if fn is None and cmd not in WGT_COMMANDS:
        raise WgtError(f"unknown command: {cmd} (see: wgt-ctl help)")
    if sel == "all":
        if fn and cmd not in ALL_OK:
            raise WgtError(f"'{cmd}' takes one server, not all")
        if not servers():
            raise WgtError("no servers yet (add one: wgt-ctl init)")
        if cmd == "log" and "--no-follow" not in args:
            args = args + ["--no-follow"]   # following one server would block the rest
        rc = 0
        for s in servers():
            out.log(s.name)
            try:
                r = fn(s, args) if fn else do_wgt(s, [cmd] + args)
            except (WgtError, OSError) as e:
                out.error(str(e))
                r = 1
            rc = rc or r
            print("")
        return rc
    s = find(sel) if sel else pick()
    if cmd == "client" and args[:1] == ["add"]:
        offer_staged_exits(s, args)
    return fn(s, args) if fn else do_wgt(s, [cmd] + args)


def main(argv: Optional[List[str]] = None) -> int:
    """wgt-ctl: the first word is a server (or all), a local command, or a
    command to run after picking a server. Returns the exit status."""
    argv = sys.argv[1:] if argv is None else argv
    try:
        if not argv or argv[0] == "list":
            cmd_list()
            return 0
        first, rest = argv[0], argv[1:]
        if first in ("help", "-h", "--help"):
            print(__doc__)
            return 0
        if first == "init":
            cmd_init(rest)
            return 0
        if first == "summary":
            return cmd_summary()
        if first == "clients":
            cmd_clients()
            return 0
        if first == "all" or first.isdigit() or any(s.name == first for s in servers()):
            if not rest:
                raise WgtError(f"usage: wgt-ctl {first} <command> (see: wgt-ctl help)")
            return on(first, rest[0], rest[1:])
        if first == "" and rest:
            # An empty server word (an unset variable in a script) picks one, as no server does.
            return on("", rest[0], rest[1:])
        if first in CTL or first in WGT_COMMANDS:
            # No server given: pick one, then run the command.
            return on("", first, rest)
        raise WgtError(f"no server or command '{first}' (servers: {', '.join(s.name for s in servers()) or 'none'}; "
                       "see: wgt-ctl help)")
    except (WgtError, OSError) as e:
        out.error(str(e))
        return 1
    except KeyboardInterrupt:
        out.error("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
