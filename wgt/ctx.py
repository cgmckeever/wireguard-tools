"""The command line (Args) and what every command works with (Ctx): the
server's paths, settings, files and a Runner. Commands import Ctx only for
type hints."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import host, store
from .config import Config, resolve
from .const import CONFIG_KEYS, Paths
from .errors import WgtError
from .system import Files, Out, Runner

# Flags that set a setting, and the setting they set.
VALUE_FLAGS = {
    "--name": "SERVER_NAME", "--subnet": "WG_SUBNET", "--subnet6": "WG_SUBNET6", "--port": "WG_PORT",
    "--mtu": "WG_MTU", "--warp-mtu": "WARP_MTU", "--dns": "WG_DNS", "--endpoint": "ENDPOINT",
    "--wg-if": "WG_IF", "--warp-if": "WARP_IF", "--table": "RT_TABLE", "--allowed-ips": "CLIENT_ALLOWED_IPS",
    # Not settings, but they take a value:
    "--exit": "EXIT_CODES", "--label": "EXIT_LABEL", "--warp": "CLIENT_WARP", "--location": "LOCATION",
    "--device-number": "DEVICE_NUMBER", "--group": "DEVICE_GROUPS",
}
BOOL_FLAGS = {
    "--dry-run": "dry_run", "--yes": "yes", "-y": "yes", "--force": "force", "--new-server-keys": "new_server_keys",
    "--purge": "purge",
    "--follow": "follow", "-f": "follow", "--no-follow": "no_follow", "--tsv": "tsv",
}

@dataclass
class Args:
    """The command line: the words, plus every flag's value."""

    words: List[str] = field(default_factory=list)
    settings: Dict[str, str] = field(default_factory=dict)
    exit_codes: str = ""
    exit_label: str = ""
    warp: str = ""              # client add --warp on|off|on,off ("" = both)
    location: str = ""          # setup --location: where this server is, for people
    device_number: str = ""     # client add: the device's static number (wgt-ctl passes the fleet's)
    groups: str = ""            # client add --group: groups for the device
    dry_run: bool = False
    yes: bool = False
    force: bool = False
    new_server_keys: bool = False
    purge: bool = False
    follow: bool = False
    no_follow: bool = False
    tsv: bool = False
    qr: Optional[bool] = None   # --qr / --no-qr; None: show it when a person is watching
    help: bool = False


def parse(argv: List[str]) -> Args:
    """Split argv into words and flags; flags may come anywhere."""
    a = Args()
    i = 0
    while i < len(argv):
        w = argv[i]
        if w in ("-h", "--help", "help") and not a.words:
            a.help = True
        elif w in BOOL_FLAGS:
            setattr(a, BOOL_FLAGS[w], True)
        elif w == "--qr":
            a.qr = True
        elif w == "--no-qr":
            a.qr = False
        elif w.startswith("--") and "=" in w and w.split("=", 1)[0] in VALUE_FLAGS:
            _set(a, w.split("=", 1)[0], w.split("=", 1)[1])
        elif w in VALUE_FLAGS:
            if i + 1 >= len(argv):
                raise WgtError(f"{w} needs a value")
            _set(a, w, argv[i + 1])
            i += 1
        elif w.startswith("-") and w != "-":
            raise WgtError(f"unknown option: {w} (see: wgt help)")
        else:
            a.words.append(w)
        i += 1
    return a


def _set(a: Args, flag: str, value: str) -> None:
    """Store a value flag: --exit/--label/--warp on Args, the rest as settings."""
    key = VALUE_FLAGS[flag]
    if key == "CLIENT_WARP":
        if value.replace(" ", "") not in ("on", "off", "on,off", "off,on", "both"):
            raise WgtError(f"--warp takes on, off or on,off: '{value}'")
        a.warp = value.replace(" ", "")
    elif key == "EXIT_CODES":
        a.exit_codes = value
    elif key == "EXIT_LABEL":
        a.exit_label = value
    elif key == "LOCATION":
        a.location = value.strip()
    elif key == "DEVICE_NUMBER":
        a.device_number = value
    elif key == "DEVICE_GROUPS":
        a.groups = value
    else:
        a.settings[key] = value


@dataclass
class Ctx:
    """Everything a command works with.

    snap is the server's files as last read: the ops helpers that write a
    database (save_client, set_disabled, state_set...) and take_lock reload
    it, so values read from it before such a call are stale after. cfg is
    resolved once at load and not reloaded (setup changes it in place)."""

    args: Args
    paths: Paths
    runner: Runner
    files: Files
    facts: host.Facts
    cfg: Config
    snap: store.Snapshot
    lock_fd: Optional[int] = None
    clock: Callable[[], float] = time.time   # "now" for handshake ages (tests set it)

    @property
    def out(self) -> Out:
        return self.runner.out

    @property
    def pub_if(self) -> str:
        return host.pub_if(self.snap.state, self.facts)

    def reload(self) -> None:
        """Re-read the server's files (after a write; a dry run's would-be
        writes included)."""
        self.snap = store.load(self.paths, self.files.read)


def load(args: Args, paths: Optional[Paths] = None, runner: Optional[Runner] = None,
         env: Optional[Dict[str, str]] = None, facts: Optional[host.Facts] = None) -> Ctx:
    """Build the Ctx: read the server's files, gather host facts, resolve the
    settings (flag > env > saved > default). Tests pass fakes for the rest."""
    paths = paths or Paths()
    runner = runner or Runner(dry_run=args.dry_run)
    runner.dry_run = runner.dry_run or args.dry_run
    facts = facts or host.gather(runner)
    snap = store.load(paths)
    saved = store.saved_settings(snap.state, list(CONFIG_KEYS) + ["ENDPOINT"])
    cfg = resolve(args.settings, os.environ if env is None else env, saved,
                  hostname=facts.hostname, routes=facts.routes4, paths=paths)
    cfg.finalize(kernel_ipv6=facts.kernel_ipv6)
    return Ctx(args, paths, runner, Files(paths, runner), facts, cfg, snap)
