"""wgt-ctl init: set up a new server, or adopt one where setup already finished."""

from __future__ import annotations

import re
import subprocess
from typing import Dict, List, Tuple

from .. import store, ui
from ..config import FLEET_SUBNETS_ENV
from ..const import SET_UP_KEYS
from ..errors import WgtError
from . import remote
from .remote import out, q


def fleet_subnets() -> List[str]:
    """The client subnets of the servers already in local/servers, so a new
    one is offered the next free subnet instead of a random one (unreachable
    servers are skipped)."""
    used = []
    for s in remote.servers():
        rc, state = remote.ssh(s.target, f"cat {remote.STATE_FILE} 2>/dev/null; true", batch=True, capture=True)
        subnet = store.state_get(state, "WG_SUBNET").strip()
        if rc == 0 and subnet:
            used.append(subnet)
    return used


def cmd_init(argv: List[str]) -> None:
    """Set up a new server (or adopt one already running these tools) and add it to the list."""
    opts, setup_args = _parse_init(argv)
    remote.ensure_local()
    address, name, target = _ask_identity(opts)
    out.log(f"Connecting to {target}")
    login(target)
    facts = probe(target, address)
    saved = facts.get("saved", "")
    if facts.get("finished") == "1":
        _adopt(name, saved, target)
        return
    if saved or facts.get("checkout") == "1":
        out.info("an unfinished setup is there; running it again")
    install_checkout(target, opts["--branch"])
    used = fleet_subnets()
    env = f"{FLEET_SUBNETS_ENV}={q(','.join(used))} " if used else ""
    out.log(f"Running wgt setup --name {name} on {address}")
    rc, _ = remote.ssh(target, f"{env}{remote.DEF_WGT} setup --name {q(name)} {q(*setup_args)}", tty=True)
    if rc != 0:
        raise WgtError(f"setup did not finish, so it isn't on the list; fix the cause and re-run: "
                       f"wgt-ctl init --address {address} "
                       f"--name {name} --user {opts['--user']} --branch {opts['--branch']}")
    record(name, target)
    out.info(f"next: wgt-ctl {name} client add <device>   (or add jumps: wgt-ctl {name} jump-add)")


def _parse_init(argv: List[str]) -> Tuple[Dict[str, str], List[str]]:
    """init's options, and what follows `--` (passed on to wgt setup)."""
    opts, setup_args, _ = remote.parse_opts("init", argv, {"--address": "", "--name": "", "--user": "root",
                                                          "--branch": "main"}, "see: wgt-ctl help")
    return opts, setup_args


def _ask_identity(opts: Dict[str, str]) -> Tuple[str, str, str]:
    """(address, name, ssh target) from the options or asked, all checked."""
    address, name = opts["--address"], opts["--name"]
    if (not address or not name) and not ui.have_tty():
        raise WgtError("init needs --address and --name without a terminal")
    address = address or ui.ask("    Server address (IP or hostname): ")
    if not address:
        raise WgtError("no address")
    name = name or ui.ask("    Server name (e.g. boba): ")
    if not remote.valid_server(name):
        raise WgtError(f"server name must be {remote.SERVER_NAME_RULE}: '{name}'")
    if any(s.name == name for s in remote.servers()):
        raise WgtError(f"'{name}' is already in {remote.LIST}")
    if not remote.BRANCH.match(opts["--branch"]):
        raise WgtError(f"bad branch name: {opts['--branch']}")
    target = f"{opts['--user']}@{address}"
    if not remote.TARGET.match(target):
        raise WgtError(f"bad --user or --address: {target}")
    return address, name, target


def login(target: str) -> None:
    """Make sure ssh works without questions: accept the host key once (the
    user compares the fingerprint), and offer to install their SSH key."""
    if remote.can_login(target):
        return
    out.info("If ssh asks about the host key, compare the fingerprint with your provider's")
    out.info("console before answering yes.")
    if subprocess.run(["ssh", *remote.SSH_FIRST_LOGIN_OPTS, target, "true"]).returncode != 0:
        raise WgtError(f"cannot log in to {target}")
    if remote.can_login(target) or not ui.yes("Log in with your SSH key from now on (ssh-copy-id)?", default=True):
        return
    if subprocess.run(["ssh-copy-id", target]).returncode != 0:
        raise WgtError("ssh-copy-id failed")


def probe(target: str, address: str) -> Dict[str, str]:
    """What init needs to know about the server: uid, OS, an existing checkout,
    a saved server name, whether setup finished there (SET_UP_KEYS). Refuses
    non-root logins and unsupported systems."""
    rc, text = remote.ssh(target, f"""echo "uid=$(id -u)"
        . /etc/os-release 2>/dev/null && echo "os=$ID $VERSION_ID"
        [ -d {remote.DEF_DIR}/.git ] && echo checkout=1
        cat {remote.STATE_FILE} 2>/dev/null; true""", capture=True)
    if rc != 0:
        raise WgtError(f"cannot reach {target}")
    facts = {k: v for k, v in (line.split("=", 1) for line in text.splitlines() if "=" in line)
             if k in ("uid", "os", "checkout")}
    if any(store.state_get(text, key) for key in SET_UP_KEYS):
        facts["finished"] = "1"
    if store.state_get(text, "SERVER_NAME"):
        facts["saved"] = store.state_get(text, "SERVER_NAME")
    if facts.get("uid") != "0":
        raise WgtError("log in as root (wgt needs it): --user root")
    osname = facts.get("os", "")
    out.info(f"{address}: {osname or 'unknown OS'}")
    if not re.match(r"^(debian|ubuntu) ", osname):
        raise WgtError(f"unsupported OS '{osname}' (Debian 11+ or Ubuntu 20.04+)")
    return facts


def _adopt(name: str, saved: str, target: str) -> None:
    """Setup finished on this server (at any version): record it under its
    saved name; never re-install."""
    if not remote.valid_server(saved):
        raise WgtError(f"the server's saved name '{saved}' isn't a valid name")
    out.log(f"wireguard-tools is already set up there as '{saved}'")
    if saved != name:
        out.info(f"recording it as '{saved}' (its saved name); rename it with: wgt-ctl {saved} setup --name {name}")
        if any(s.name == saved for s in remote.servers()):
            raise WgtError(f"'{saved}' is already in {remote.LIST}")
    record(saved, target)
    out.info(f"update it to this version with: wgt-ctl {saved} pull")


def install_checkout(target: str, branch: str) -> None:
    """git (if missing) and a checkout of <branch> at remote.DEF_DIR."""
    url, d = remote.repo_url(), remote.DEF_DIR
    out.log(f"Installing git and cloning {url} ({branch}) to {d}")
    rc, _ = remote.ssh(target, f"""set -e
        export DEBIAN_FRONTEND=noninteractive
        command -v git >/dev/null || {{ apt-get update -qq && apt-get install -y -qq git >/dev/null; }}
        if [ -d {remote.DEF_DIR}/.git ]; then
            git -C {d} fetch -q && git -C {d} checkout -q {q(branch)} && git -C {d} pull --ff-only -q
        else git clone -q -b {q(branch)} {q(url)} {d}; fi
        git -C {d} log --oneline -1""")
    if rc != 0:
        raise WgtError(f"clone of {url} ({branch}) failed on {target}")


def require_branch(target: str, branch: str) -> None:
    """<branch> can be cloned from <target>. Run before anything is deleted."""
    url = remote.repo_url()
    if remote.ssh(target, f"git ls-remote --exit-code {q(url, branch)} >/dev/null")[0] != 0:
        raise WgtError(f"{target} can't see branch '{branch}' of {url}; nothing has changed")


def record(name: str, target: str) -> None:
    """Add a server to local/servers, with its exits dir."""
    remote.ensure_local()
    with open(remote.LIST, "a") as f:
        f.write(f"{name:<10} {target}\n")
    remote.private_dir(remote.exits_dir(name))
    out.log(f"Added '{name}' ({target}) to {remote.LIST}")
