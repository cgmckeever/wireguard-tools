"""`wgt`: the server-side command. Parses flags (they may come anywhere on the
line), loads the server's settings and files, and dispatches to the command
modules; `wgt log` and the hidden `wgt debug` live here too."""

from __future__ import annotations

import os
import sys
from typing import Callable, List, Optional

from . import host, ops, render, rules, store
from .const import ALL_WATCHDOGS, DEVICE_ADDRESS_FORM, EXIT_WATCHDOG, WATCHDOGS
from .ctx import Args, Ctx, load, parse, settings_env
from .errors import WgtError
from .system import Out, Runner

USAGE = f"""\
Usage: wgt [options] <command> ...

A WireGuard server for your devices. Each device (client) gets tunnel configs
named [warp.][<jump>.]<device>: out of this server or a jump, with or without
Cloudflare WARP. The device picks one at a time.

  setup                      install or update everything
  summary | status | test    at a glance / everything running / all checks
  log [N] [--no-follow]      changes, watchdogs and tunnels: last N lines, then follows (Ctrl-C stops;
                             --no-follow, or piped: print and stop)
  backup [dir] | restore <archive> [--old-names] | rollback [--purge]

  client list [--tsv] | add <device> [--exit host|<jump>[,..]] [--warp on|off|on,off] [--allowed-ips ..] [--group ..]
         show <config> [--qr] | share <config> | rotate <config> [--allowed-ips ..]
         rename <old> <new> | disable <config> | enable <config> | remove <config>
         (add: per exit, <device> and warp.<device> (--warp on|off: one); a full config
          name adds just that; rotate, remove, disable, enable, show, share: none picks)
  device list [--tsv] | set <device> [number] | rename <device> <new> [--force]
                             a device's static address {DEVICE_ADDRESS_FORM}, whichever config it's using
         group [<device> <group,group>] | ungroup <device> [group,group]
                             who it can reach: devices sharing a group; "server" reaches the server
                             and not the others in it; no groups: isolated
  jump   list | add <code> <wg.conf|-> [--label NAME] | remove [code] | test   (old name: exit)
  tunnel test                every way out (each exit, with and without WARP), traced
  warp rotate [host|<jump>|all]   new WARP account (keys, identity): this server's (default),
                             a jump's, or all; device configs don't change

Options: --dry-run, --yes/-y, --purge, --qr/--no-qr
  --force             setup: reinstall wgcf, regenerate the WARP profile, take over
                      configs these tools didn't create; device rename: finish one that stopped half way
  --old-names         restore: an archive from before config names said WARP
  --new-server-keys   setup: new server keypair (every client must then be rotated)
Settings (for setup and restore; --allowed-ips also on client): --name --subnet --subnet6 --port --mtu --warp-mtu --dns
  --endpoint --wg-if --warp-if --table --allowed-ips --location
"""


# --- commands defined here (the rest live in their modules; see COMMANDS) ---

def cmd_debug(ctx: Ctx, what: List[str]) -> None:
    """Hidden: print what this checkout renders from the server's state (keys
    redacted), so tools/render-diff.sh can compare two versions before a deploy.
    From the functions setup writes with, or "identical" there wouldn't mean what setup
    would do (test_invariants holds it to that)."""
    from . import exits, server, units, warp
    from .system import redact
    target = what[0] if what else "all"
    pieces = []
    if target in ("all", "wg0"):
        pieces.append(("wg0", ops.render_wg_conf(ctx)))
    if target in ("all", "warp"):
        pieces.append(("warp", warp.conf_text(ctx, store.read_text(ctx.paths.wgcf_profile()) or "")))
    if target in ("all", "exits"):
        for e in ctx.snap.exits:
            pieces.append((e.px, exits.jump_tunnel_conf(ctx, e)))
            profile = store.read_text(ctx.paths.wgcf_profile(ctx.paths.exit_wgcf_dir(e.code))) or ""
            pieces.append((e.wx, warp.conf_text(ctx, profile, e)))
    if target in ("all", "hooks"):
        down = rules.hook_cmds(ctx.cfg, ctx.snap, ctx.pub_if, up=False)
        pieces.append(("hooks-down", "".join(x + "\n" for x in down)))
        ipt = rules.ipt_rules(ctx.cfg, ctx.pub_if, ctx.snap.exits)
        pieces.append(("ipt", "".join("|".join(r) + "\n" for r in ipt)))
    if target in ("all", "units"):
        pieces += [(os.path.basename(path), text) for path, text in units.unit_files(ctx)]
        pieces.append(("dropin", render.render_dropin(ctx.cfg)))
    if target in ("all", "system"):
        pieces.append(("sysctl", render.render_sysctl(server.sysctl_wanted(ctx)[0])))
        pieces.append(("packages", "".join(p + "\n" for p in server.PACKAGES)))
    for name, text in pieces:
        sys.stdout.write(f"=== {name}\n{redact(text)}")


def cmd_log(ctx: Ctx, words: List[str]) -> None:
    """One time-ordered log: these tools' own steps, both watchdogs, and the
    wg-quick units (wg0, warp, each jump's tunnels). Like tail -f: the
    last [lines] (default 40), then new entries until Ctrl-C. With
    --no-follow, or when the output isn't a terminal, it prints and stops."""
    ops.no_more_words(words[1:], "wgt log [lines] [--no-follow]")
    count = words[0] if words else "40"
    if not store.is_uint(count):
        raise WgtError("usage: wgt log [lines] [--no-follow]")
    matches: List[str] = []
    for tag in ("wg-tools", *ALL_WATCHDOGS):
        matches += [f"SYSLOG_IDENTIFIER={tag}", "+"]
    ifcs = [ctx.cfg.WG_IF, ctx.cfg.WARP_IF]
    for e in ctx.snap.exits:
        ifcs += [e.px, e.wx]
    for ifc in ifcs:
        matches += [f"_SYSTEMD_UNIT=wg-quick@{ifc}.service", "+", f"UNIT=wg-quick@{ifc}.service", "+"]
    follow = ctx.args.follow or (sys.stdout.isatty() and not ctx.args.no_follow)
    cmd = ["journalctl", "--no-pager", "-o", "short-iso", "-n", count] + (["-f"] if follow else []) + matches[:-1]
    try:
        os.execvp(cmd[0], cmd)
    except OSError as e:
        raise WgtError(f"cannot run journalctl: {e.strerror}") from None


# command -> (module, function); a module is only imported when its command runs.
# The watchdogs run every 10s and still reach most modules (exits, warp, units), so
# anything slow to import is imported where it's used: see warp._fetch.
COMMANDS = {
    "setup": ("server", "cmd_setup"), "backup": ("backup", "cmd_backup"),
    "restore": ("backup", "cmd_restore"), "rollback": ("server", "cmd_rollback"),
    "summary": ("checks", "cmd_summary"),
    "status": ("checks", "cmd_status"), "test": ("checks", "cmd_test"), "log": ("cli", "cmd_log"),
    "client": ("clients", "cmd_client"), "device": ("clients", "cmd_device"),
    "jump": ("exits", "cmd_exit"), "exit": ("exits", "cmd_exit"),
    "tunnel": ("exits", "cmd_tunnel"), "warp": ("warp_rotate", "cmd_warp"),
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
        env = settings_env(args, cmd, os.environ)
        if cmd == "watchdog":
            return run_watchdog(args, rest, out)
        if os.environ.get("WGT_SKIP_PREFLIGHT") != "1":
            host.preflight(Runner())
        ctx = load(args, env=env)
        if args.dry_run:
            out.log("DRY RUN: nothing will be changed")
        return command(cmd)(ctx, rest) or 0
    except (WgtError, OSError) as e:
        out.error(str(e))
        return 1
    except KeyboardInterrupt:
        out.error("interrupted")
        return 130


def run_watchdog(args: Args, rest: List[str], out: Out) -> int:
    """A watchdog run (every 10-30s from a timer): no host checks beyond root,
    no fact gathering, and any error is logged to the journal, not a traceback."""
    word = (rest or [""])[0]
    tag = next((w.name for w in WATCHDOGS if w.word == word), EXIT_WATCHDOG)   # any watchdog's, for a bad word's error
    if os.geteuid() != 0 and os.environ.get("WGT_SKIP_PREFLIGHT") != "1":
        raise WgtError("must run as root")
    quick = host.Facts(kernel_ipv6=os.path.exists("/proc/net/if_inet6"), has_ipv6=False,
                       hostname="", routes4="", default_route="")
    try:
        ctx = load(args, env={}, facts=quick)
        return command("watchdog")(ctx, rest) or 0
    except Exception as e:   # a timer job must log, not crash (a bad database line too: `wgt log` shows it)
        from .system import journal
        journal(Runner(), tag, f"error: {type(e).__name__}: {e}")
        out.error(f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
