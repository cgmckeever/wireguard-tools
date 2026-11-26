"""Jumps from staged files: ~/wg-tunnels/fleet/servers/<server>/carriers/*.conf (tunnels.py),
pushed to a server. Each file names its jump in a "# jump: <code> <label>" line,
written the first time it's pushed."""

from __future__ import annotations

import os
import re
import tempfile
from typing import List, Optional, Sequence, Tuple

from .. import render, store, ui
from ..errors import WgtError
from . import fleet_cmds, home, remote, tunnels
from .remote import Server, out, q

JUMP = re.compile(r"(?m)^# jump: (\S+) (.+?)[ \t]*$")


def link_of(path: str) -> Optional[Tuple[str, str]]:
    """(code, label) from a staged file's "# jump:" line, once it has been named."""
    m = JUMP.search(store.read_text(path) or "")
    return (m.group(1), m.group(2)) if m else None


def with_jump_line(text: str, code: str, label: str) -> str:
    """The config with its "# jump:" line first (any earlier one replaced)."""
    return render.with_first_line(text, "# jump: ", f"# jump: {code} {label}")


def make_link(server: str, path: str, on_server: Sequence[str] = ()) -> Tuple[str, str]:
    """First push of a staged config: ask what it is and pick its code (the name's
    first 3 letters + a country tag like Proton's "FR#252", e.g. par-fr), then write
    both into the file. A code another of this server's files already uses, or a
    jump the server already has (<on_server>: the push would replace it), is refused."""
    file = os.path.basename(path)
    text = store.read_text(path) or ""
    hint = render.peer_hint(text)
    if not ui.have_tty():
        raise WgtError(f"{file} isn't named yet; run jump-add in a terminal once to name it")
    name = ui.ask(f"    What is {file} ({hint or 'no hint'})? e.g. Paris, France: ")
    if not name:
        raise WgtError(f"no name for {file}")
    m = re.match(r"^([A-Za-z]{2})#", hint)
    city = re.sub(r"[^a-z]", "", name.lower())[:3]
    code = city + (f"-{m.group(1).lower()}" if m else "")
    staged = {link[0] for f in tunnels.staged(server) if f != path for link in [link_of(f)] if link}
    taken = staged | set(on_server)
    while True:
        reply = ui.ask(f"    Short code on the server [{code}]: ", gone="\0")
        # Ctrl-D or a lost terminal stops here; otherwise a taken default would ask forever.
        if reply == "\0" or (not reply and not store.new_exit_code(code)):
            raise WgtError("no code given")
        code = reply or code
        if not store.new_exit_code(code):
            out.info(f"the code must be {store.EXIT_CODE_RULE}")
        elif code in taken:
            whose = f"{server} already has a jump '{code}'" if code in on_server \
                else f"another of {server}'s staged files is jump '{code}'"
            out.info(f"{whose}; pick another")
        else:
            break
    remote.write_local(path, with_jump_line(text, code, name))
    return code, name


def with_local_file_comment(text: str, file: str) -> str:
    """The config with a "# local file: <name>" line naming where it came from
    (any earlier one replaced)."""
    return render.with_first_line(text, "# local file: ", f"# local file: {file}")


def add_one_exit(s: Server, path: str) -> None:
    """Copy one local config to the server and add it there as its jump;
    the uploaded copy is deleted once imported."""
    file = os.path.basename(path)
    # Every push, from jump-add or client add's offer: the folder and file hold private keys.
    os.chmod(os.path.dirname(path), 0o700)
    os.chmod(path, 0o600)
    link = link_of(path)
    if link:
        code, label = link
        if not store.valid_exit_code(code):
            raise WgtError(f"bad code '{code}' in {file}'s '# jump:' line")
        confirm_replace(s, code, file)
    else:
        # Without a terminal make_link refuses before asking anything: no need to ask the server.
        # A code it picks is never one the server has: nothing to replace.
        code, label = make_link(s.name, path, remote_exit_codes(s) if ui.have_tty() else ())
    out.log(f"{s.name}: {file} -> jump {code} ({label})")
    if remote.ssh(s.target, f"install -d -m 700 {remote.REMOTE.exit_dir} {remote.INBOX}")[0] != 0:
        raise WgtError(f"cannot reach {s.target}")
    uploaded = f"{remote.INBOX}/{code}.conf"
    fd, tmp = tempfile.mkstemp()
    try:
        # As store.read_text read it: any non-UTF-8 byte goes up as it was, not a traceback.
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(with_local_file_comment(store.read_text(path) or "", file))
        os.chmod(tmp, 0o600)
        if not remote.scp(tmp, f"{s.target}:{uploaded}"):
            raise WgtError("upload failed")
    finally:
        os.remove(tmp)
    rc, _ = remote.ssh(s.target, f"{s.wgt} jump add {q(code, uploaded, '--label', label, '--yes')}; "
                                 f"rc=$?; rm -f {q(uploaded)}; exit $rc")
    if rc != 0:
        raise WgtError(f"jump add failed for {code}")
    tunnels.after(s, "jump", ["add"])   # a replaced jump's label is in its configs' "# exit:" line


def confirm_replace(s: Server, code: str, file: str) -> None:
    """Pushing <file> as jump <code> replaces the server's jump of that code (`jump add
    --yes`), moving its clients to the new tunnel: when that jump came from another file
    (its "# local file:" line, or none: added on the server), ask first."""
    if code not in remote_exit_codes(s):
        return
    source = jump_file_of(s, code)
    if source == file:
        return   # this file again: an update
    was = f"from {source}" if source else "added on the server"
    if not ui.have_tty():
        raise WgtError(f"{s.name} already has jump {code} ({was}): "
                       f"run jump-add in a terminal to replace it with {file}")
    if not ui.yes(f"{s.name}'s jump {code} is {was}; replace it with {file} (its clients move to it)?", False):
        raise WgtError(f"jump {code} kept")


def jump_file_of(s: Server, code: str) -> str:
    """The local file jump <code> on the server was pushed from ("" if none is named)."""
    path = remote.q(remote.REMOTE.jump_source(code))
    rc, o = remote.ssh(s.target, f"sed -n 's/^# local file: //p' {path} 2>/dev/null | head -n 1; true",
                       batch=True, capture=True)
    if rc != 0:
        raise WgtError(f"can't reach {s.name} to see its jump {code}")
    return o.strip()


def remote_exit_codes(s: Server) -> List[str]:
    """The jump codes the server has."""
    rc, o = remote.ssh(s.target, f"cat {remote.REMOTE.exit_db} 2>/dev/null; true", batch=True, capture=True)
    if rc != 0:
        raise WgtError(f"can't reach {s.name} to see its jumps")
    return [e.code for e in store.parse_exits(o)]


def print_exit_files(files: List[str], on_server: List[str]) -> None:
    """The staged configs, numbered: file, code and name (once named), on the server or not."""
    w = max(4, *(len(os.path.basename(f)) for f in files))
    print("")
    print(f"  {'#':>3}  {'FILE':<{w}}  {'CODE':<7} {'NAME':<20} ON SERVER")
    for i, f in enumerate(files, 1):
        code, label = link_of(f) or ("-", "(not named yet)")
        print(f"  {i:>3}  {os.path.basename(f):<{w}}  {code:<7} {label:<20} {'yes' if code in on_server else 'no'}")
    print("")


def offer_staged_exits(s: Server, args: List[str]) -> None:
    """Before `client add` in a terminal: if exit configs are staged here but
    not on the server, list them and offer to push one, so the server's exit
    menu can offer it. Skipped when the exit is already given."""
    name = fleet_cmds.client_name(args)
    # Nothing to offer when the exit is given (--exit, or a <code>.<name>
    # client name), there must be no prompts (--yes), or nothing may change (--dry-run).
    if remote.flag_given(args, "--exit") or "." in name \
            or any(a in remote.YES for a in remote.wgt_words(args)) or remote.dry_run(args):
        return
    files = tunnels.staged(s.name)
    if not files or not ui.have_tty():
        return
    on_server = remote_exit_codes(s)
    staged = [f for f in files if (link_of(f) or ("", ""))[0] not in on_server]
    if not staged:
        return
    out.log(f"Jump configs staged here but not on {s.name}")
    print_exit_files(staged, on_server)
    sel = ui.ask("    Push one first, so the new client can use it? [number, Enter skips]: ")
    if not sel:
        return
    if not store.is_uint(sel) or not 1 <= int(sel) <= len(staged):
        raise WgtError(f"no staged config {sel}")
    add_one_exit(s, staged[int(sel) - 1])


def do_exit_add(s: Server, args: List[str]) -> int:
    """Push one of this server's local configs (by its number in the list) or all."""
    if len(args) > 1:   # `jump-add 1 2` must not push only 1
        raise WgtError(f"usage: wgt-ctl {s.name} jump-add [n|all] (got: {' '.join(args)})")
    src = home.carriers_dir(s.name)
    files = tunnels.staged(s.name)
    if not files:
        raise WgtError(f"no configs in {home.shown(src)} (save WireGuard configs there, any name ending .conf)")
    sel = args[0] if args else ""
    if not sel:
        if not ui.have_tty():
            raise WgtError(f"no config given (and no terminal to pick one): wgt-ctl {s.name} jump-add <n|all>")
        print_exit_files(files, remote_exit_codes(s))
        sel = ui.ask("  Add which? [number or all]: ")
    if not sel:
        raise WgtError("nothing chosen")
    if sel == "all":
        picked = files
    elif store.is_uint(sel) and 1 <= int(sel) <= len(files):
        picked = [files[int(sel) - 1]]
    else:
        raise WgtError(f"no config number '{sel}' (1-{len(files)}, or all)")
    for f in picked:
        add_one_exit(s, f)
    out.log(f"{s.name} jumps")
    return remote.do_wgt(s, ["jump", "list"])
