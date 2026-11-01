"""`wgt-ctl`: drives the servers listed in local/servers from your own machine
(macOS or Linux). The server is the first word:

  wgt-ctl                               the servers: branch@commit (or unreachable), exits staged
  wgt-ctl init [--address A] [--name N] [--user U] [--branch B] [-- setup flags]
                                        set up a new server (or adopt one already running these tools)
  wgt-ctl clients                       every client on every server
  wgt-ctl <server|all> <wgt command>    run it there, e.g. wgt-ctl boba client add phone --exit par-fr
  wgt-ctl <server|all> pull | backup    git pull / back up and fetch the archive to local/backups/<server>/
  wgt-ctl <server> exit-add [n|file|all] | ssh | branch <b> | forget [--yes]

<server> is a name or list number; with none, pick from the list. Private
files stay under local/ (untracked): servers, exits/<server>/*.conf (Proton
configs, any name), exits/details.tsv (which file is which exit), backups/."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import List, Optional, Tuple

from . import render, store, ui
from .errors import WgtError
from .system import Out

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL = os.path.join(ROOT, "local")
LIST = os.path.join(LOCAL, "servers")
LINKS = os.path.join(LOCAL, "exits", "details.tsv")   # server, local file, code, full name (tab-separated)
DEF_DIR = "/opt/wireguard-tools"
INBOX = "/etc/wireguard/exits/incoming"
SSH_OPTS = ["-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15"]
RESERVED = ("init", "clients", "all", "help", "list")
CTL_COMMANDS = ("pull", "backup", "exit-add", "ssh", "branch", "forget")

out = Out()


@dataclass
class Server:
    name: str
    target: str
    dir: str = DEF_DIR

    @property
    def wgt(self) -> str:
        return shlex.quote(f"{self.dir}/bin/wgt")


# --- the server list ---

def ensure_local() -> None:
    for d in (LOCAL, os.path.join(LOCAL, "exits"), os.path.join(LOCAL, "backups")):
        os.makedirs(d, exist_ok=True)
        os.chmod(d, 0o700)
    if not os.path.exists(LIST):
        with open(LIST, "w") as f:
            f.write("# Servers this machine manages (untracked): name  ssh-target  [checkout]\n")
    os.chmod(LIST, 0o600)


def servers() -> List[Server]:
    out_: List[Server] = []
    for line in (store.read_text(LIST) or "").splitlines():
        f = line.split()
        if f and not f[0].startswith("#"):
            out_.append(Server(f[0], f[1], f[2] if len(f) > 2 else DEF_DIR))
    return out_


def find(sel: str) -> Server:
    for i, s in enumerate(servers(), 1):
        if sel in (s.name, str(i)):
            return s
    raise WgtError(f"no server '{sel}' in {LIST}")


def pick() -> Server:
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
    return bool(re.match(r"^[A-Za-z0-9_-]{1,32}$", name)) and name not in RESERVED


# --- ssh ---

def ssh(target: str, command: str, tty: bool = False, batch: bool = False, capture: bool = False,
        input: Optional[str] = None) -> Tuple[int, str]:
    """Run <command> on <target>. tty: with a terminal when we have one
    (prompts, QR codes); batch: never prompt, never read stdin (probes)."""
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
            rc, o = ssh(s.target, f"printf '%s@%s' \"$(git -C {dq} rev-parse --abbrev-ref HEAD)\" "
                                  f"\"$(git -C {dq} rev-parse --short HEAD)\"", batch=True, capture=True)
            probe = o.strip() if rc == 0 else "unreachable"
        print(f"  {i:>2}) {s.name:<10} {s.target:<24} {probe:<22} {'exits staged: ' + staged if staged else ''}".rstrip())
    print("")


def cmd_init(argv: List[str]) -> None:
    """Set up a new server (or adopt one already running these tools) and add it to the list."""
    opts = {"--address": "", "--name": "", "--user": "root", "--branch": "main"}
    setup_args: List[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--":
            setup_args = argv[i + 1:]
            break
        if argv[i] in opts and i + 1 < len(argv):
            opts[argv[i]] = argv[i + 1]
            i += 2
            continue
        raise WgtError(f"init: unknown option {argv[i]}")
    ensure_local()
    address, name = opts["--address"], opts["--name"]
    if (not address or not name) and not ui.have_tty():
        raise WgtError("init needs --address and --name without a terminal")
    address = address or ui.ask("Server address (IP or hostname): ")
    if not address:
        raise WgtError("no address")
    name = name or ui.ask("Server name (e.g. boba): ")
    if not valid_server(name):
        raise WgtError(f"server name must be 1-32 of [A-Za-z0-9_-] (and not {', '.join(RESERVED)}): '{name}'")
    if any(s.name == name for s in servers()):
        raise WgtError(f"'{name}' is already in {LIST}")
    target = f"{opts['--user']}@{address}"
    out.log(f"Connecting to {target}")
    if not can_login(target):
        out.info("If ssh asks about the host key, compare the fingerprint with your provider's")
        out.info("console before answering yes.")
        if subprocess.run(["ssh"] + SSH_OPTS + ["-o", "BatchMode=no", target, "true"]).returncode != 0:
            raise WgtError(f"cannot log in to {target}")
        if not can_login(target):
            reply = ui.ask("    Log in with your SSH key from now on (ssh-copy-id)? [Y/n] ")
            if not reply or reply.lower().startswith("y"):
                if subprocess.run(["ssh-copy-id", target]).returncode != 0:
                    raise WgtError("ssh-copy-id failed")
    rc, probe = ssh(target, f"""echo "uid=$(id -u)"
        . /etc/os-release 2>/dev/null && echo "os=$ID $VERSION_ID"
        [ -x {DEF_DIR}/bin/wgt ] && echo installed=1
        sed -n "s/^SERVER_NAME=/saved=/p" /etc/wireguard/wg-tools.state 2>/dev/null | tail -n 1""", capture=True)
    if rc != 0:
        raise WgtError(f"cannot reach {target}")
    facts = dict(line.split("=", 1) for line in probe.splitlines() if "=" in line)
    if facts.get("uid") != "0":
        raise WgtError("log in as root (wgt needs it): --user root")
    osname = facts.get("os", "")
    out.info(f"{address}: {osname or 'unknown OS'}")
    if not re.match(r"^(debian|ubuntu) ", osname):
        raise WgtError(f"unsupported OS '{osname}' (Debian 11+ or Ubuntu 20.04+)")
    saved = facts.get("saved", "")
    if facts.get("installed") == "1" and saved:
        if not valid_server(saved):
            raise WgtError(f"the server's saved name '{saved}' isn't a valid name")
        out.log(f"wireguard-tools is already set up there as '{saved}'")
        if saved != name:
            out.info(f"recording it as '{saved}' (its saved name); rename it with: wgt-ctl {saved} setup --name {name}")
            name = saved
            if any(s.name == name for s in servers()):
                raise WgtError(f"'{name}' is already in {LIST}")
        record(name, target)
        return
    url = repo_url()
    branch = opts["--branch"]
    out.log(f"Installing git and cloning {url} ({branch}) to {DEF_DIR}")
    rc, _ = ssh(target, f"""set -e
        export DEBIAN_FRONTEND=noninteractive
        command -v git >/dev/null || {{ apt-get update -qq && apt-get install -y -qq git >/dev/null; }}
        if [ -d {DEF_DIR}/.git ]; then git -C {DEF_DIR} fetch -q && git -C {DEF_DIR} checkout -q {q(branch)} && git -C {DEF_DIR} pull -q
        else git clone -q -b {q(branch)} {q(url)} {DEF_DIR}; fi
        git -C {DEF_DIR} log --oneline -1""")
    if rc != 0:
        raise WgtError("clone failed")
    out.log(f"Running wgt setup --name {name} on {address}")
    rc, _ = ssh(target, f"{DEF_DIR}/bin/wgt setup --name {q(name)} {q(*setup_args)}", tty=True)
    if rc != 0:
        raise WgtError(f"setup did not finish; fix the cause and re-run: wgt-ctl init --address {address} --name {name}")
    record(name, target)
    out.info(f"next: wgt-ctl {name} client add <client>   (or add exit cities: wgt-ctl {name} exit-add)")


def record(name: str, target: str) -> None:
    ensure_local()
    with open(LIST, "a") as f:
        f.write(f"{name:<10} {target}\n")
    d = os.path.join(LOCAL, "exits", name)
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o700)
    out.log(f"Added '{name}' ({target}) to {LIST}")


def repo_url() -> str:
    p = subprocess.run(["git", "-C", ROOT, "remote", "get-url", "origin"], stdout=subprocess.PIPE, text=True)
    url = p.stdout.strip()
    if p.returncode != 0 or not url:
        raise WgtError(f"no git remote 'origin' in {ROOT}")
    m = re.match(r"^git@([^:]+):(.+)$", url)   # servers clone anonymously
    return f"https://{m.group(1)}/{m.group(2)}" if m else url


def cmd_clients() -> None:
    """Every client on every server in one table."""
    fmt = "  {:<10} {:<20} {:<6} {:<15} {:<14} {:<12} {}"
    print("")
    print(fmt.format("SERVER", "NAME", "EXIT", "ADDRESS", "LAST SEEN", "RX/TX MiB", "ALLOWED IPS"))
    for s in servers():
        rc, o = ssh(s.target, f"{s.wgt} client list --tsv", batch=True, capture=True)
        if rc != 0:
            note = "(its tools predate wgt; pull it)" if can_login(s.target) else "(unreachable)"
            print(f"  {s.name:<10} {note}")
            continue
        rows = [line.split("\t") for line in o.splitlines() if line]
        if not rows:
            print(f"  {s.name:<10} (no clients)")
        for r in rows:
            print(fmt.format(s.name, *(r + [""] * 6)[:6]))
    print("")


# --- per-server commands ---

def do_wgt(s: Server, args: List[str]) -> int:
    """Any wgt command, run there. Printed scp hints use this machine's login."""
    env = f"WGT_SSH_TARGET={shlex.quote(s.target)}"
    missing = (f"echo 'ERROR: {s.name} has no {s.dir}/bin/wgt (it runs the older bash tools); "
               f"update it: wgt-ctl {s.name} pull' >&2; exit 1")
    return ssh(s.target, f"if [ -x {s.wgt} ]; then {env} {s.wgt} {q(*args)}; else {missing}; fi", tty=True)[0]


def do_pull(s: Server, args: List[str]) -> int:
    d = shlex.quote(s.dir)
    return ssh(s.target, f"git -C {d} pull -q && git -C {d} log --oneline -1 | sed 's/^/    /'")[0]


def do_branch(s: Server, args: List[str]) -> int:
    if not args:
        raise WgtError("usage: wgt-ctl <server> branch <branch>")
    d = shlex.quote(s.dir)
    return ssh(s.target, f"set -e; git -C {d} fetch -q; git -C {d} checkout -q {q(args[0])}; git -C {d} pull -q; "
                         f"printf '    %s: ' \"$(git -C {d} rev-parse --abbrev-ref HEAD)\"; git -C {d} log --oneline -1")[0]


def do_ssh(s: Server, args: List[str]) -> int:
    return subprocess.run(["ssh"] + SSH_OPTS + [s.target]).returncode


def do_backup(s: Server, args: List[str]) -> int:
    """wgt backup there, then fetch the archive into local/backups/<server>/."""
    rc, o = ssh(s.target, f"{s.wgt} backup --yes >/dev/null || exit 1; "
                          "ls -t /var/backups/wireguard-tools/wireguard-tools.*.tar.gz 2>/dev/null | head -n 1", capture=True)
    archive = o.strip()
    if rc != 0 or not archive:
        out.error(f"{s.name}: backup failed")
        return 1
    ensure_local()
    d = os.path.join(LOCAL, "backups", s.name)
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o700)
    if not scp(f"{s.target}:{archive}", d + "/"):
        out.error(f"{s.name}: fetching {archive} failed")
        return 1
    local = os.path.join(d, os.path.basename(archive))
    os.chmod(local, 0o600)
    out.info(f"{s.name}: {local} (private keys inside; restore with wgt restore)")
    return 0


def do_forget(s: Server, args: List[str]) -> int:
    if "--yes" not in args and "-y" not in args:
        reply = ui.ask(f"    Remove '{s.name}' from {LIST}? (nothing on the server changes) [y/N] ")
        if not reply.lower().startswith("y"):
            out.info("kept")
            return 0
    lines = [line for line in (store.read_text(LIST) or "").splitlines() if (line.split() or [""])[0] != s.name]
    with open(LIST, "w") as f:
        f.write("\n".join(lines) + "\n")
    out.info(f"forgot '{s.name}'; its local/exits/{s.name} and local/backups/{s.name} stay")
    return 0


# --- exit cities from local files ---

def link_of(server: str, file: str) -> Optional[Tuple[str, str]]:
    """(code, full name) for a local exit config, if it has been named."""
    for line in (store.read_text(LINKS) or "").splitlines():
        f = line.split("\t")
        if len(f) >= 4 and f[0] == server and f[1] == file:
            return f[2], f[3]
    return None


def make_link(server: str, path: str) -> Tuple[str, str]:
    """First push of a local config: ask what it is and pick its code (city's
    first 3 letters + Proton's country tag, e.g. par-fr). A code another of
    this server's files already uses is refused."""
    file = os.path.basename(path)
    hint = render.proton_hint(store.read_text(path) or "")
    if not ui.have_tty():
        raise WgtError(f"{file} isn't named yet; run exit-add in a terminal once to name it")
    name = ui.ask(f"What is {file} (Proton {hint or '?'})? e.g. Paris, France: ")
    if not name:
        raise WgtError(f"no name for {file}")
    m = re.match(r"^([A-Za-z]{2})#", hint)
    city = re.sub(r"[^a-z]", "", name.lower())[:3]
    code = city + (f"-{m.group(1).lower()}" if m else "")
    taken = {f.split("\t")[2] for f in (store.read_text(LINKS) or "").splitlines()
             if len(f.split("\t")) >= 4 and f.split("\t")[0] == server}
    while True:
        code = ui.ask(f"Short code on the server [{code}]: ") or code
        if not store.valid_exit_code(code):
            print("  2-6 of a-z0-9 and -, starting with a letter (e.g. par-fr)")
        elif code in taken:
            print(f"  {server} already has an exit '{code}'; pick another")
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
    """Copy one local config to the server and add it there as its exit city;
    the uploaded copy is deleted once imported."""
    file = os.path.basename(path)
    os.chmod(path, 0o600)
    link = link_of(s.name, file) or make_link(s.name, path)
    code, label = link
    if not store.valid_exit_code(code):
        raise WgtError(f"bad code '{code}' for {file} in {LINKS}")
    out.log(f"{s.name}: {file} -> exit {code} ({label})")
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
    rc, _ = ssh(s.target, f"{s.wgt} exit add {q(code, f'{INBOX}/{code}.conf', '--label', label, '--yes')}; "
                          f"rc=$?; rm -f {INBOX}/{code}.conf; exit $rc")
    if rc != 0:
        raise WgtError(f"exit add failed for {code}")


def do_exit_add(s: Server, args: List[str]) -> int:
    """Pick from this server's local configs (number, file, or all)."""
    src = os.path.join(LOCAL, "exits", s.name)
    files = sorted(os.path.join(src, f) for f in os.listdir(src) if f.endswith(".conf")) if os.path.isdir(src) else []
    if not files:
        raise WgtError(f"no configs in {src} (save Proton WireGuard configs there, any name ending .conf)")
    os.chmod(src, 0o700)
    sel = args[0] if args else ""
    if not sel:
        if not ui.have_tty():
            raise WgtError(f"no config given (and no terminal to pick one): wgt-ctl {s.name} exit-add <n|file|all>")
        rc, o = ssh(s.target, "awk '!/^#/ { print $1 }' /etc/wireguard/exits.db 2>/dev/null", batch=True, capture=True)
        remote = set(o.split())
        w = max(4, *(len(os.path.basename(f)) for f in files))
        print("")
        print(f"  {'#':>3}  {'FILE':<{w}}  {'CODE':<7} {'NAME':<20} ON SERVER")
        for i, f in enumerate(files, 1):
            link = link_of(s.name, os.path.basename(f))
            code, label = link if link else ("-", "(not named yet)")
            print(f"  {i:>3}  {os.path.basename(f):<{w}}  {code:<7} {label:<20} {'yes' if code in remote else 'no'}")
        print("")
        sel = ui.ask("  Add which? [number, file, or all]: ")
    if not sel:
        raise WgtError("nothing chosen")
    if sel == "all":
        picked = files
    elif sel.isdigit() and 1 <= int(sel) <= len(files):
        picked = [files[int(sel) - 1]]
    elif os.path.isfile(os.path.join(src, sel if sel.endswith(".conf") else sel + ".conf")):
        picked = [os.path.join(src, sel if sel.endswith(".conf") else sel + ".conf")]
    else:
        raise WgtError(f"no config '{sel}' in {src}")
    for f in picked:
        add_one_exit(s, f)
    out.log(f"{s.name} exits")
    return do_wgt(s, ["exit", "list"])


CTL = {"pull": do_pull, "backup": do_backup, "exit-add": do_exit_add, "ssh": do_ssh, "branch": do_branch,
       "forget": do_forget}
ALL_OK = ("pull", "backup", "branch")   # ctl commands that take "all" (every wgt command does)


def on(sel: str, cmd: str, args: List[str]) -> int:
    """Run a command on one server, or (with "all") on each in turn: one failing
    doesn't stop the rest; the result is 1 if any failed."""
    fn = CTL.get(cmd)
    if sel == "all":
        if fn and cmd not in ALL_OK:
            raise WgtError(f"'{cmd}' takes one server, not all")
        rc = 0
        for s in servers():
            out.log(s.name)
            try:
                r = fn(s, args) if fn else do_wgt(s, [cmd] + args)
            except WgtError as e:
                out.error(str(e))
                r = 1
            rc = rc or r
            print("")
        return rc
    s = find(sel) if sel else pick()
    return fn(s, args) if fn else do_wgt(s, [cmd] + args)


def main(argv: Optional[List[str]] = None) -> int:
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
        if first == "clients":
            cmd_clients()
            return 0
        if first == "all" or any(s.name == first for s in servers()) or first.isdigit():
            if not rest:
                raise WgtError(f"usage: wgt-ctl {first} <command> (see: wgt-ctl help)")
            return on(first, rest[0], rest[1:])
        if first in CTL or not first.startswith("-"):
            # No server given: pick one, then run the command.
            return on("", first, rest)
        raise WgtError(f"unknown: {first} (see: wgt-ctl help)")
    except WgtError as e:
        out.error(str(e))
        return 1
    except KeyboardInterrupt:
        out.error("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
