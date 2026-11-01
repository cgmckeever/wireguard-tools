"""`wgt`: the server-side command. Parses flags (anywhere on the line, as the
bash tools did), loads the server's settings and files, and dispatches to
the command modules."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import host, render, rules, store
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
    "--exit": "EXIT_CODES", "--label": "EXIT_LABEL",
}
BOOL_FLAGS = {
    "--dry-run": "dry_run", "--yes": "yes", "-y": "yes", "--force": "force", "--new-server-keys": "new_server_keys",
    "--purge": "purge",
    "--follow": "follow", "-f": "follow", "--no-follow": "no_follow", "--tsv": "tsv",
}

USAGE = """\
Usage: wgt [options] <command> ...

A WireGuard server whose clients exit through Cloudflare WARP or this
server's own IP (switchable), or per client through an exit city.

  setup                      install or update everything
  status | test              what's running / all checks
  log [N] [--no-follow]      changes, watchdogs and tunnels: last N lines, then follows (Ctrl-C stops)
  warp [on|off]              where clients exit (live)
  backup [dir] | restore <archive> | rollback [--purge]

  client list | add <name> [--exit <code>[,..]] | show <name> [--qr] | share <name>
         rotate <name> | rename <old> <new> | disable <name> | enable <name> | remove <name>
  exit   list | add <code> <file|-> [--label NAME] | remove <code> | test

Options: --dry-run, --yes/-y, --purge, --qr/--no-qr
  --force             setup: reinstall wgcf, regenerate the WARP profile, take over
                      configs these tools didn't create
  --new-server-keys   setup: new server keypair (every client must then be rotated)
Settings (saved by setup): --name --subnet --subnet6 --port --mtu --warp-mtu --dns
  --endpoint --wg-if --warp-if --table --allowed-ips
"""


@dataclass
class Args:
    """The command line: the words, plus every flag's value."""

    words: List[str] = field(default_factory=list)
    settings: Dict[str, str] = field(default_factory=dict)
    exit_codes: str = ""
    exit_label: str = ""
    dry_run: bool = False
    yes: bool = False
    force: bool = False
    new_server_keys: bool = False
    purge: bool = False
    follow: bool = False
    no_follow: bool = False
    tsv: bool = False
    qr: int = 0     # 1 = --qr, -1 = --no-qr, 0 = decide (on if a terminal)
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
            a.qr = 1
        elif w == "--no-qr":
            a.qr = -1
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
    """Store a value flag: --exit/--label on Args, the rest as settings."""
    key = VALUE_FLAGS[flag]
    if key == "EXIT_CODES":
        a.exit_codes = value
    elif key == "EXIT_LABEL":
        a.exit_label = value
    else:
        a.settings[key] = value


@dataclass
class Ctx:
    """Everything a command works with."""

    args: Args
    paths: Paths
    runner: Runner
    files: Files
    facts: host.Facts
    cfg: Config
    snap: store.Snapshot
    lock_fd: Optional[int] = None

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


# --- commands defined here (the rest live in their modules; see COMMANDS) ---

def cmd_debug(ctx: Ctx, what: List[str]) -> None:
    """Hidden: print what this version renders from the server's state, for
    tools/parity (keys redacted)."""
    from .system import redact
    target = what[0] if what else "all"
    psk = store.psk_reader(ctx.paths)
    pieces = []
    if target in ("all", "wg0"):
        pieces.append(("wg0", render.render_wg_conf(ctx.cfg, ctx.snap, ctx.pub_if, psk)))
    if target in ("all", "warp"):
        profile = store.read_text(ctx.paths.wgcf_profile()) or ""
        pieces.append(("warp", render.render_warp_conf(ctx.cfg, profile, ctx.facts.has_ipv6, host.resolve4)))
    if target in ("all", "exits"):
        for e in ctx.snap.exits:
            raw = store.read_text(ctx.paths.proton_conf(e.code)) or ""
            pieces.append((f"px-{e.code}", render.render_proton_conf(raw, e)))
            prof = store.read_text(ctx.paths.wgcf_profile(ctx.paths.exit_wgcf_dir(e.code))) or ""
            pieces.append((f"wx-{e.code}", render.render_warp_conf(
                ctx.cfg, prof, ctx.facts.has_ipv6, host.resolve4, table=rules.exit_wtable(e), mark=rules.exit_mark(e))))
    if target in ("all", "hooks"):
        pieces.append(("hooks-down", "".join(x + "\n" for x in rules.hook_cmds(ctx.cfg, ctx.snap, ctx.pub_if, "down"))))
        ipt = rules.ipt_rules(ctx.cfg, ctx.pub_if, ctx.snap.exits)
        pieces.append(("ipt", "".join("|".join(r) + "\n" for r in ipt)))
    for name, text in pieces:
        sys.stdout.write(f"=== {name}\n{redact(text)}")


def cmd_log(ctx: Ctx, words: List[str]) -> None:
    """One time-ordered log: these tools' own steps, both watchdogs, and the
    wg-quick units (wg0, warp, each exit city's tunnels). Like tail -f: the
    last [lines] (default 40), then new entries until Ctrl-C. With
    --no-follow, or when the output isn't a terminal, it prints and stops."""
    from .const import EXIT_WATCHDOG, WATCHDOG
    count = words[0] if words else "40"
    if not count.isdigit():
        raise WgtError("usage: wgt log [lines] [--no-follow]")
    matches: List[str] = []
    for tag in ("wg-tools", WATCHDOG, EXIT_WATCHDOG):
        matches += [f"SYSLOG_IDENTIFIER={tag}", "+"]
    ifcs = [ctx.cfg.WG_IF, ctx.cfg.WARP_IF]
    for e in ctx.snap.exits:
        ifcs += [f"px-{e.code}", f"wx-{e.code}"]
    for ifc in ifcs:
        matches += [f"_SYSTEMD_UNIT=wg-quick@{ifc}.service", "+", f"UNIT=wg-quick@{ifc}.service", "+"]
    follow = ctx.args.follow or (sys.stdout.isatty() and not ctx.args.no_follow)
    cmd = ["journalctl", "--no-pager", "-o", "short-iso", "-n", count] + (["-f"] if follow else []) + matches[:-1]
    try:
        os.execvp(cmd[0], cmd)
    except OSError as e:
        raise WgtError(f"cannot run journalctl: {e.strerror}") from None


# command -> (module, function); a module is only imported when its command runs
# (the watchdogs run every 10s and need little).
COMMANDS = {
    "setup": ("server", "cmd_setup"), "warp": ("server", "cmd_warp"), "backup": ("server", "cmd_backup"),
    "restore": ("server", "cmd_restore"), "rollback": ("server", "cmd_rollback"),
    "status": ("checks", "cmd_status"), "test": ("checks", "cmd_test"), "log": ("cli", "cmd_log"),
    "client": ("clients", "cmd_client"), "exit": ("exits", "cmd_exit"),
    "watchdog": ("watchdog", "cmd_watchdog"), "debug": ("cli", "cmd_debug"),
}


def command(name: str) -> Callable[[Ctx, List[str]], Optional[int]]:
    """The function behind a command (its module is imported only when used)."""
    import importlib
    mod, fn = COMMANDS[name]
    return getattr(importlib.import_module(f"wgt.{mod}"), fn)


def main(argv: Optional[List[str]] = None) -> int:
    """Parse, check the host, load the server's state and run the command; its
    return value (None = 0) is the exit status."""
    out = Out()
    os.umask(0o077)   # keys and configs are private unless a file sets its own mode
    try:
        args = parse(sys.argv[1:] if argv is None else argv)
        if args.help or not args.words:
            sys.stdout.write(USAGE)
            return 0 if args.help else 1
        cmd, rest = args.words[0], args.words[1:]
        if cmd not in COMMANDS:
            raise WgtError(f"unknown command: {cmd} (see: wgt help)")
        if cmd == "watchdog":
            return run_watchdog(args, rest, out)
        if os.environ.get("WGT_SKIP_PREFLIGHT") != "1":
            host.preflight()
        ctx = load(args)
        if args.dry_run:
            out.log("DRY RUN: nothing will be changed")
        return command(cmd)(ctx, rest) or 0
    except WgtError as e:
        out.error(str(e))
        return 1
    except KeyboardInterrupt:
        out.error("interrupted")
        return 130


def run_watchdog(args: Args, rest: List[str], out: Out) -> int:
    """A watchdog run (every 10-30s from a timer): no host checks beyond root,
    no fact gathering, and any error is logged to the journal, not a traceback."""
    from .const import EXIT_WATCHDOG, WATCHDOG
    tag = WATCHDOG if rest[:1] == ["warp"] else EXIT_WATCHDOG
    if os.geteuid() != 0 and os.environ.get("WGT_SKIP_PREFLIGHT") != "1":
        raise WgtError("must run as root")
    quick = host.Facts(kernel_ipv6=os.path.exists("/proc/net/if_inet6"), has_ipv6=False,
                       hostname="", routes4="", default_route="")
    try:
        ctx = load(args, facts=quick)
        return command("watchdog")(ctx, rest) or 0
    except WgtError:
        raise
    except Exception as e:   # a timer job must log, not crash
        from .system import journal
        journal(Runner(), tag, f"error: {type(e).__name__}: {e}")
        out.error(f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
