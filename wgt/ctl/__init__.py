"""`wgt-ctl`: drives the servers listed in local/servers from your own machine
(macOS or Linux). Most commands are  wgt-ctl SERVER COMMAND  where SERVER is
a name, a list number, or all; a few take no server (below).

One server:
  wgt-ctl SERVER <wgt command>          run it there, e.g. wgt-ctl boba client add phone --exit par-fr
  wgt-ctl SERVER pull | backup          git pull (fast-forward) / back up, fetched to local/backups/SERVER/
  wgt-ctl SERVER jump-add [n|all]       push a WireGuard config from local/exits/SERVER/ as a jump
  wgt-ctl SERVER ssh [command] | branch <b>
  wgt-ctl SERVER forget [--yes]         drop it from local/servers (the server is untouched)
  wgt-ctl SERVER remove [--yes]         tear wireguard-tools off it (rollback --purge, delete its
                                        checkout, wgcf and archives), then forget it
  wgt-ctl SERVER move [--to ADDRESS] [--user U] [--branch B] [--yes] [-- setup flags]
                                        rebuild it from its own backup, on a new machine or in
                                        place: backup, down, restore (with the setup flags), test,
                                        then (--to) point the list at it and tear the old one down

Every server, in turn:
  wgt-ctl all <wgt command> | pull | backup | branch <b>

No server:
  wgt-ctl [list]                        the servers: branch@commit (or unreachable), jumps staged
  wgt-ctl summary                       every server at a glance (wgt summary on each)
  wgt-ctl clients                       every config on every server
  wgt-ctl devices                       each device's static address, and what doesn't line up
  wgt-ctl devices sync                  give numbers to devices that have none (the same on every server)
  wgt-ctl devices fix <device> <n>      set a device's number on every server that has it
  wgt-ctl devices forget <device>       delete a removed device's line (its number can be reused)
  wgt-ctl init [--address A] [--name N] [--user U] [--branch B] [-- setup flags]
                                        set up a new server (or adopt one already running these tools)

Leave SERVER out of a server command to pick one from the list. Private
files stay under local/ (untracked): servers, exits/<server>/*.conf (jump
WireGuard configs, any name), exits/details.tsv (which file is which jump),
devices (each device's static number), backups/.

`wgt-ctl SERVER client add` gives a device its fleet-wide number, and
`wgt-ctl all device rename <device> <new> [--force]` renames a whole device on
every server (--force: even with a server unreachable; re-run it to finish)."""

from __future__ import annotations

import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Tuple

from ..errors import WgtError
from . import fleet_cmds, jumpfiles, newserver, rebuild, remote
from .remote import Server, out, q


def cmd_summary() -> int:
    """`wgt summary` from every server, one block each, fetched in parallel
    and shown in list order. 1 if any server couldn't answer."""
    def fetch(s: Server) -> Tuple[int, str]:
        return remote.ssh(s.target, f"{s.wgt} summary 2>&1", batch=True, capture=True)

    todo = remote.servers()
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
        why = "unreachable" if code == 255 else last
        print(f"{s.name}  ({s.target})")
        print(f"    {why}")
    print("")
    return rc


def do_pull(s: Server, args: List[str]) -> int:
    """git pull its checkout (fast-forward only). A pull changes no running
    config; when rendering changed (tools/render-diff.sh shows it), run setup."""
    remote.no_args("pull", args)
    d = shlex.quote(s.dir)
    # --ff-only: a checkout with local commits or edits stops with git's message
    # instead of merging on a production server.
    return remote.ssh(s.target, f"git -C {d} pull --ff-only -q && git -C {d} log --oneline -1 | sed 's/^/    /'")[0]


def do_branch(s: Server, args: List[str]) -> int:
    """Switch its checkout to <branch>, then pull."""
    if len(args) != 1 or not remote.BRANCH.match(args[0]):
        raise WgtError("usage: wgt-ctl <server> branch <branch>")
    d = shlex.quote(s.dir)
    return remote.ssh(s.target, f"set -e; git -C {d} fetch -q; git -C {d} checkout -q {q(args[0])}; "
                         f"git -C {d} pull --ff-only -q; "
                         f"printf '    %s: ' \"$(git -C {d} rev-parse --abbrev-ref HEAD)\"; "
                         f"git -C {d} log --oneline -1")[0]


def do_ssh(s: Server, args: List[str]) -> int:
    """Log in (or run <args> there as a command)."""
    return subprocess.run(["ssh"] + remote.SSH_OPTS + [s.target] + ([" ".join(args)] if args else [])).returncode


CTL = {"pull": do_pull, "backup": rebuild.do_backup, "jump-add": jumpfiles.do_exit_add, "ssh": do_ssh,
       "branch": do_branch, "forget": rebuild.do_forget, "remove": rebuild.do_remove, "move": rebuild.do_move}


ALL_OK = ("pull", "backup", "branch")   # ctl commands that take "all" (every wgt command does)


def on(sel: str, cmd: str, args: List[str]) -> int:
    """Run a command on the server <sel> ("": pick one), or on each for "all"."""
    if cmd not in CTL and cmd not in remote.WGT_COMMANDS:
        raise WgtError(f"unknown command: {cmd} (see: wgt-ctl help)")
    return _on_all(cmd, args) if sel == "all" else _on_one(remote.find(sel) if sel else remote.pick(), cmd, args)


def _run(s: Server, cmd: str, args: List[str]) -> int:
    fn = CTL.get(cmd)
    return fn(s, args) if fn else remote.do_wgt(s, [cmd] + args)


def _on_one(s: Server, cmd: str, args: List[str]) -> int:
    new_number_for = ""
    if cmd == "client" and args[:1] == ["add"]:
        jumpfiles.offer_staged_exits(s, args)
        args, new_number_for = fleet_cmds.numbered_add(args)
    rc = _run(s, cmd, args)
    if rc != 0 and new_number_for:
        fleet_cmds.forget_number(new_number_for)
    return rc


def _on_all(cmd: str, args: List[str]) -> int:
    """Each server in turn: one failing doesn't stop the rest. The status is the
    first failure's, or 0."""
    if cmd in CTL and cmd not in ALL_OK:
        raise WgtError(f"'{cmd}' takes one server, not all")
    if not remote.servers():
        raise WgtError("no servers yet (add one: wgt-ctl init)")
    if cmd in ("client", "device") and args[:1] == ["rename"]:
        return fleet_cmds.rename_everywhere(args[1:])
    new_number_for = ""
    if cmd == "client" and args[:1] == ["add"]:
        args, new_number_for = fleet_cmds.numbered_add(args)
    if cmd == "log" and "--no-follow" not in args:
        args = args + ["--no-follow"]   # following one server would block the rest
    results = []
    for s in remote.servers():
        out.log(s.name)
        try:
            results.append(_run(s, cmd, args))
        except (WgtError, OSError) as e:
            out.error(str(e))
            results.append(1)
        print("")
    if new_number_for and all(results):
        fleet_cmds.forget_number(new_number_for)
    return next((r for r in results if r), 0)


def main(argv: Optional[List[str]] = None) -> int:
    """wgt-ctl: the first word is a server (or all), a local command, or a
    command to run after picking a server. Returns the exit status."""
    argv = sys.argv[1:] if argv is None else argv
    try:
        if not argv or argv[0] == "list":
            remote.cmd_list()
            return 0
        first, rest = argv[0], argv[1:]
        if first in ("help", "-h", "--help"):
            print(__doc__)
            return 0
        if first == "init":
            newserver.cmd_init(rest)
            return 0
        if first == "summary":
            return cmd_summary()
        if first == "clients":
            fleet_cmds.cmd_clients()
            return 0
        if first == "devices":
            return fleet_cmds.cmd_devices(rest)
        if first == "all" or first.isdigit() or any(s.name == first for s in remote.servers()):
            if not rest:
                raise WgtError(f"usage: wgt-ctl {first} <command> (see: wgt-ctl help)")
            return on(first, rest[0], rest[1:])
        if first == "" and rest:
            # An empty server word (an unset variable in a script) picks one, as no server does.
            return on("", rest[0], rest[1:])
        if first in CTL or first in remote.WGT_COMMANDS:
            # No server given: pick one, then run the command.
            return on("", first, rest)
        names = ", ".join(s.name for s in remote.servers()) or "none"
        raise WgtError(f"no server or command '{first}' (servers: {names}; "
                       "see: wgt-ctl help)")
    except (WgtError, OSError) as e:
        out.error(str(e))
        return 1
    except KeyboardInterrupt:
        out.error("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
