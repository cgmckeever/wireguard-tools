"""Jumps from local files: local/exits/<server>/*.conf, pushed to a server."""

from __future__ import annotations

import os
import re
import tempfile
from typing import List, Optional, Tuple

from .. import render, store, ui
from ..errors import WgtError
from . import fleet_cmds, remote
from .remote import Server, out, q


def links() -> List[Tuple[str, str, str, str]]:
    """local/exits/details.tsv: (server, local file, code, full name) per named config."""
    rows = [line.split("\t") for line in (store.read_text(remote.LINKS) or "").splitlines()]
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
    with open(remote.LINKS, "a") as f:
        f.write(f"{server}\t{file}\t{code}\t{name}\n")
    os.chmod(remote.LINKS, 0o600)
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
        raise WgtError(f"bad code '{code}' for {file} in {remote.LINKS}")
    out.log(f"{s.name}: {file} -> jump {code} ({label})")
    if remote.ssh(s.target, f"install -d -m 700 {remote.REMOTE.exit_dir} {remote.INBOX}")[0] != 0:
        raise WgtError(f"cannot reach {s.target}")
    fd, tmp = tempfile.mkstemp()
    try:
        with os.fdopen(fd, "w") as f:
            f.write(with_local_file_comment(store.read_text(path) or "", file))
        os.chmod(tmp, 0o600)
        if not remote.scp(tmp, f"{s.target}:{remote.INBOX}/{code}.conf"):
            raise WgtError("upload failed")
    finally:
        os.remove(tmp)
    uploaded = f"{remote.INBOX}/{code}.conf"
    rc, _ = remote.ssh(s.target, f"{s.wgt} jump add {q(code, uploaded, '--label', label, '--yes')}; "
                                 f"rc=$?; rm -f {uploaded}; exit $rc")
    if rc != 0:
        raise WgtError(f"jump add failed for {code}")


def local_exit_files(s: Server) -> List[str]:
    """This server's staged jump configs (local/exits/<server>/*.conf)."""
    src = os.path.join(remote.LOCAL, "exits", s.name)
    return sorted(os.path.join(src, f) for f in os.listdir(src) if f.endswith(".conf")) if os.path.isdir(src) else []


def remote_exit_codes(s: Server) -> List[str]:
    """The jump codes the server has."""
    rc, o = remote.ssh(s.target, f"cat {remote.REMOTE.exit_db} 2>/dev/null; true", batch=True, capture=True)
    if rc != 0:
        raise WgtError(f"can't reach {s.name} to see its jumps")
    return [e.code for e in store.parse_exits(o)]


def print_exit_files(s: Server, files: List[str], on_server: List[str]) -> None:
    """The staged configs, numbered: file, code and name (once named), on the server or not."""
    w = max(4, *(len(os.path.basename(f)) for f in files))
    print("")
    print(f"  {'#':>3}  {'FILE':<{w}}  {'CODE':<7} {'NAME':<20} ON SERVER")
    for i, f in enumerate(files, 1):
        link = link_of(s.name, os.path.basename(f))
        code, label = link if link else ("-", "(not named yet)")
        print(f"  {i:>3}  {os.path.basename(f):<{w}}  {code:<7} {label:<20} {'yes' if code in on_server else 'no'}")
    print("")


def offer_staged_exits(s: Server, args: List[str]) -> None:
    """Before `client add` in a terminal: if exit configs are staged here but
    not on the server, list them and offer to push one, so the server's exit
    menu can offer it. Skipped when the exit is already given."""
    name = fleet_cmds.client_name(args)
    # Nothing to offer when the exit is given (--exit, or a <code>.<name>
    # client name) or there must be no prompts (--yes).
    if "--exit" in args or any(a.startswith("--exit=") for a in args) or "." in name \
            or any(a in remote.YES for a in args):
        return
    files = local_exit_files(s)
    if not files or not ui.have_tty():
        return
    on_server = remote_exit_codes(s)
    staged = [f for f in files if (link_of(s.name, os.path.basename(f)) or ("", ""))[0] not in on_server]
    if not staged:
        return
    out.log(f"Jump configs staged here but not on {s.name}")
    print_exit_files(s, staged, on_server)
    sel = ui.ask("    Push one first, so the new client can use it? [number, Enter skips]: ")
    if not sel:
        return
    if not store.is_uint(sel) or not 1 <= int(sel) <= len(staged):
        raise WgtError(f"no staged config {sel}")
    add_one_exit(s, staged[int(sel) - 1])


def do_exit_add(s: Server, args: List[str]) -> int:
    """Push one of this server's local configs (by its number in the list) or all."""
    src = os.path.join(remote.LOCAL, "exits", s.name)
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
    elif store.is_uint(sel) and 1 <= int(sel) <= len(files):
        picked = [files[int(sel) - 1]]
    else:
        raise WgtError(f"no config number '{sel}' (1-{len(files)}, or all)")
    for f in picked:
        add_one_exit(s, f)
    out.log(f"{s.name} jumps")
    return remote.do_wgt(s, ["jump", "list"])
