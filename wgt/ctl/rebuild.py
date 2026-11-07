"""Taking a server down or rebuilding it from its backup: backup, forget, remove, move."""

from __future__ import annotations

import os
import shlex
from typing import Dict, List, Tuple

from .. import store, ui
from ..errors import WgtError
from . import newserver, remote
from .remote import Server, out, q


def do_backup(s: Server, args: List[str]) -> int:
    """wgt backup there, then fetch the archive into local/backups/<server>/."""
    remote.no_args("backup", args)
    local = fetch_backup(s)
    out.info(f"{s.name}: {local} (private keys inside; restore with wgt restore)")
    return 0


def fetch_backup(s: Server) -> str:
    """wgt backup there; the archive fetched into local/backups/<server>/ (its path here)."""
    rc, o = remote.ssh(s.target, f"{s.wgt} backup --yes >/dev/null || exit 1; "
                          f"ls -t {remote.REMOTE.backup_dir}/wireguard-tools.*.tar.gz 2>/dev/null | head -n 1",
                capture=True)
    archive = o.strip()
    if rc != 0 or not archive:
        raise WgtError(f"{s.name}: backup failed")
    remote.ensure_local()
    d = remote.private_dir(os.path.join(remote.LOCAL, "backups", s.name))
    if not remote.scp(f"{s.target}:{archive}", d + "/"):
        raise WgtError(f"{s.name}: fetching {archive} failed")
    local = os.path.join(d, os.path.basename(archive))
    os.chmod(local, 0o600)
    return local


def _refuse_shallow(checkout: str) -> None:
    if checkout.rstrip("/").count("/") < 2:
        raise WgtError(f"refusing to delete checkout '{checkout}' (too close to /)")


def do_remove(s: Server, args: List[str]) -> int:
    """Tear wireguard-tools off a server: rollback --purge (keys, clients,
    WARP accounts, jumps, rules, units; original files restored), then
    its checkout, the wgt link, wgcf and the backup archives. Packages stay.
    Only after all that succeeds is it forgotten here. Asks for the server's
    name unless --yes."""
    sure = remote.only_yes("remove", args)
    _refuse_shallow(s.dir)
    out.warn(f"this removes wireguard-tools from {s.name} ({s.target}): its keys, clients, WARP accounts, "
             f"jumps and backup archives, and {s.dir}. Its clients stop working. Irreversible "
             f"(want a copy first? wgt-ctl {s.name} backup)")
    if not sure:
        remote.needs_terminal("remove")
        if ui.ask(f"    Type the server's name ({s.name}) to remove it: ") != s.name:
            out.info("kept")
            return 0
    if teardown(s.target, s.dir) != 0:
        raise WgtError(f"teardown failed on {s.name}; it stays in {remote.LIST} - fix the cause and re-run")
    return do_forget(s, ["--yes"])


def teardown(target: str, checkout: str) -> int:
    """rollback --purge there, then delete the checkout, the wgt link, wgcf
    and the backup archives. set -e: if rollback fails, nothing is deleted."""
    _refuse_shallow(checkout)
    d = shlex.quote(checkout)
    return remote.ssh(target, f"""set -e
        if [ -x {d}/bin/wgt ]; then {d}/bin/wgt rollback --purge --yes; fi
        rm -rf {d} {remote.REMOTE.backup_dir}
        rm -f {remote.REMOTE.wgcf_bin} {remote.REMOTE.wgt_link}
        echo "    removed {checkout}, {remote.REMOTE.wgt_link}, {remote.REMOTE.wgcf_bin} and {remote.REMOTE.backup_dir}"
        echo "    (packages left installed)"
        """, tty=True)[0]


def do_forget(s: Server, args: List[str]) -> int:
    """Drop it from local/servers; nothing on the server changes."""
    if not remote.only_yes("forget", args):
        remote.needs_terminal("forget")
        if not ui.yes(f"Remove '{s.name}' from {remote.LIST}? (nothing on the server changes)", default=False):
            out.info("kept")
            return 0
    lines = [line for line in (store.read_text(remote.LIST) or "").splitlines() if (line.split() or [""])[0] != s.name]
    remote.write_local(remote.LIST, "\n".join(lines) + "\n")
    out.info(f"forgot '{s.name}'; its local/exits/{s.name} and local/backups/{s.name} stay")
    return 0


def parse_move(args: List[str]) -> Tuple[Dict[str, str], List[str], bool]:
    """move's options, what follows `--` (setup flags for the restore), and --yes."""
    opts = {"--to": "", "--user": "", "--branch": "main"}
    sure = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return opts, args[i + 1:], sure
        if a in remote.YES:
            sure = True
            i += 1
            continue
        if a not in opts:
            raise WgtError(f"unknown option: {a} (usage: wgt-ctl <server> move [--to ADDRESS] [--user U] "
                           "[--branch B] [--yes] [-- setup flags])")
        if i + 1 >= len(args):
            raise WgtError(f"move: {a} needs a value")
        opts[a] = args[i + 1]
        i += 2
    return opts, [], sure


def do_move(s: Server, args: List[str]) -> int:
    """Rebuild a server from its own backup, on a new machine (--to) or in
    place, with setup flags after `--` (e.g. -- --endpoint vpn.example.com):
    back up and fetch; prepare the new machine; bring the old one down; restore
    there; test. Passed: the server list points at the new machine and the old
    one is torn down (asked). Failed: the old one can be brought back."""
    opts, setup_args, sure = parse_move(args)
    if s.dir != remote.DEF_DIR:
        raise WgtError(f"move needs the checkout at {remote.DEF_DIR} (this one is {s.dir})")
    if not remote.BRANCH.match(opts["--branch"]):
        raise WgtError(f"bad branch name: {opts['--branch']}")
    user = opts["--user"] or s.target.split("@", 1)[0]
    in_place = not opts["--to"]
    new = s.target if in_place else f"{user}@{opts['--to']}"
    if not remote.TARGET.match(new):
        raise WgtError(f"bad --user or --to: {new}")
    if not in_place and new == s.target:
        raise WgtError("--to is this server's own address; leave it out to rebuild in place")
    if in_place:
        newserver.require_branch(s.target, opts["--branch"])   # a purge can't be undone by a typo's clone
    how = "rebuild it in place" if in_place else f"move it to {new}"
    out.log(f"{s.name}: {how}" + (f", with setup {' '.join(setup_args)}" if setup_args else ""))
    steps = ["back up and fetch the archive here",
             "re-clone the checkout (after step 3)" if in_place else "prepare the new machine (install, no setup)",
             f"bring {s.name} down" + (" and purge it" if in_place else " (rollback; keys kept)"),
             "restore it" + ("" if in_place else " on the new machine"),
             "wgt test and wgt tunnel test",
             "done" if in_place else "point the server list at it; tear the old machine down (asked)"]
    for i, step in enumerate(steps, 1):
        out.info(f"{i}. {step}")
    out.warn(f"{s.name}'s devices are cut off from step 3 until step 4 finishes")
    if not sure:
        remote.needs_terminal("move")
        if ui.ask(f"    Type the server's name ({s.name}) to go ahead: ") != s.name:
            out.info("nothing changed")
            return 0

    out.log(f"1. Backing up {s.name}")
    archive = fetch_backup(s)
    out.info(f"saved: {archive}")

    if not in_place:
        out.log(f"2. Preparing {new}")
        newserver.login(new)
        facts = newserver.probe(new, opts["--to"])
        if facts.get("saved"):
            raise WgtError(f"{new} is already set up as '{facts['saved']}'; tear it down first "
                           f"(nothing has changed on {s.name})")
        newserver.install_checkout(new, opts["--branch"])

    out.log(f"3. Bringing {s.name} down")
    down = f"{s.wgt} rollback --purge --yes" if in_place else f"{s.wgt} rollback --yes"
    if remote.ssh(s.target, down, tty=True)[0] != 0:
        if in_place:   # part of the purge may have run: the archive is the way back
            return _move_failed(s, in_place, archive, "rollback failed")
        raise WgtError(f"rollback failed on {s.name}; it may be half down - bring it back with: "
                       f"wgt-ctl {s.name} setup --yes")
    if in_place:
        if remote.ssh(s.target, f"rm -rf {remote.DEF_DIR}")[0] != 0:
            return _move_failed(s, in_place, archive, f"could not remove {remote.DEF_DIR}")
        try:
            newserver.install_checkout(new, opts["--branch"])
        except WgtError as e:
            return _move_failed(s, in_place, archive, str(e))

    out.log("4. Restoring" + ("" if in_place else f" on {new}"))
    uploaded = f"/root/{os.path.basename(archive)}"
    if not remote.scp(archive, f"{new}:{uploaded}"):
        return _move_failed(s, in_place, archive, "upload failed")
    rc, _ = remote.ssh(new, f"{remote.DEF_DIR}/bin/wgt restore {q(uploaded)} --yes {q(*setup_args)}; rc=$?; "
                     f"rm -f {q(uploaded)}; exit $rc", tty=True)
    if rc != 0:
        return _move_failed(s, in_place, archive, f"restore failed (exit {rc})")

    out.log("5. Testing")
    tested = remote.ssh(new, f"{remote.DEF_DIR}/bin/wgt test && {remote.DEF_DIR}/bin/wgt tunnel test", tty=True)[0]
    if tested != 0:
        return _move_failed(s, in_place, archive, "tests failed")

    if in_place:
        out.log(f"{s.name} rebuilt")
        return 0
    remote.set_target(s.name, new)
    out.log(f"6. {s.name} is now {new} in {remote.LIST}")
    out.info(f"the old machine ({s.target}) is down, its keys kept")
    # No by default: Enter, or a lost terminal, must not destroy the way back.
    # Its tests can't see a provider's firewall blocking the new machine's UDP port.
    if sure or ui.yes(f"Tear wireguard-tools off the old machine ({s.target}) now?", default=False):
        if teardown(s.target, s.dir) != 0:
            out.warn(f"teardown failed on {s.target}; run there: wgt rollback --purge")
    else:
        out.info(f"later: ssh {s.target} wgt rollback --purge (it shares WARP and jump keys with the new one)")
    return 0


def _move_failed(s: Server, in_place: bool, archive: str, why: str) -> int:
    """A move stopped after the old server went down: say how to recover, and
    offer to bring the old machine back (a move to a new one only)."""
    out.error(f"{s.name}: {why}")
    if in_place:
        wgt = f"{remote.DEF_DIR}/bin/wgt"
        out.info(f"the backup is here: {archive}. Once the cause is fixed, on {s.name}: clone the tools to "
                 f"{remote.DEF_DIR} "
                 f"if they're gone, copy the archive there, then: {wgt} rollback --purge (if it got part-way); "
                 f"{wgt} restore <archive>")
        return 1
    out.info(f"the old machine ({s.target}) is down but intact; the new one is left as it is to look at")
    if ui.have_tty() and ui.yes("Bring the old machine back up now (wgt setup there)?", default=True):
        remote.ssh(s.target, f"{s.wgt} setup --yes", tty=True)
    else:
        out.info(f"bring it back with: wgt-ctl {s.name} setup --yes")
    return 1
