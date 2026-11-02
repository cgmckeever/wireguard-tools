"""The server's lifecycle: setup, warp on|off and rollback (setup's inverse,
kept beside it). Backup and restore are in backup.py.

Setup installs or updates everything: the WARP tunnel, then wg0 with the
saved WARP setting (on for a new install). WARP failing on a first install
leaves it off; on an existing install with WARP on it stays on (fail closed:
clients are blocked, not leaked) until fixed or switched off."""

from __future__ import annotations

import copy
import glob
import os
import re
from typing import TYPE_CHECKING, List, Optional

from . import checks, exits, host, live, net, ops, render, rules, store, ui, units, warp
from .const import CONFIG_KEYS, EXIT_WATCHDOG, MARKER, WATCHDOG
from .errors import WgtError
from .system import settings_of, write_private

if TYPE_CHECKING:
    from .ctx import Ctx

PACKAGES = ("wireguard", "wireguard-tools", "iptables", "curl", "ca-certificates", "iputils-ping",
            "qrencode", "magic-wormhole")


# --- small pieces ---

def run_hooks(ctx: Ctx, up: bool) -> None:
    """Run wg0's PostUp/PostDown lines directly (rollback, error cleanup)."""
    ctx.runner.sh_lines(rules.hook_cmds(ctx.cfg, ctx.snap, ctx.pub_if, up))


def install_packages(ctx: Ctx) -> None:
    """apt-get install whichever of PACKAGES are missing."""
    missing = [p for p in PACKAGES
               if ctx.runner.query(["dpkg-query", "-W", "-f=${Status}", p]).strip() != "install ok installed"]
    if not missing:
        ctx.out.info("packages present")
        return
    ctx.runner.step(f"Install packages: {' '.join(missing)}", f"apt-get remove {' '.join(missing)}")
    ctx.runner.run(["apt-get", "update"])
    ctx.runner.run(["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y",
                    "--no-install-recommends"] + missing)


def guard_default_route(ctx: Ctx) -> None:
    """Abort (and stop warp) if the default route moved: SSH depends on it."""
    if ctx.runner.dry_run:
        return
    rec = store.state_get(ctx.snap.state, "DEFAULT_ROUTE")
    now = (ctx.runner.query(["ip", "-4", "route", "show", "default"]).splitlines() or [""])[0]
    if host.route_key(now) != host.route_key(rec):
        ctx.out.warn(f"default route changed (was: {rec}, now: {now})")
        ctx.runner.run(["systemctl", "stop", f"wg-quick@{ctx.cfg.WARP_IF}"], check=False)
        raise WgtError("default route changed; investigate before re-running")


def start_unit(ctx: Ctx, ifc: str, changed: bool) -> None:
    """Enable an interface's wg-quick unit for boot and make it match its config."""
    r, unit = ctx.runner, f"wg-quick@{ifc}"
    enabled = False
    if not live.if_exists(r, ifc):
        r.step(f"Bring up {ifc} ({unit}, enabled at boot)", f"systemctl disable --now {unit}")
        ops.confirm(ctx, f"Bring up {ifc} now?", default_yes=True)   # the setup is useless without it
        r.run(["systemctl", "enable", "--now", unit])
        enabled = True
    elif changed:
        r.step(f"Restart {unit} to apply its new config",
               f"restore the .bak in {ctx.paths.change_dir}, then systemctl restart {unit}")
        ops.confirm(ctx, f"Restart {ifc} now?")
        if live.unit_active(r, unit):
            r.run(["systemctl", "restart", unit])
        else:
            r.run(["wg-quick", "down", ifc])
            r.run(["systemctl", "enable", "--now", unit])
            enabled = True
    else:
        ctx.out.info(f"{ifc} is up and unchanged")
        if not live.unit_active(r, unit):
            ctx.out.warn(f"{ifc} is up but not via {unit} (started by hand?)")
    if not enabled and not live.unit_enabled(r, unit):
        r.run(["systemctl", "enable", unit])


def link_wgt(ctx: Ctx) -> None:
    """/usr/local/sbin/wgt -> this checkout's bin/wgt, so it runs from anywhere."""
    link = ctx.paths.wgt_link
    target = units.checkout_wgt()
    if os.path.islink(link) and os.readlink(link) == target:
        return
    ctx.runner.step(f"Link {link} -> {target}", f"rm {link}")
    ctx.runner.run(["ln", "-sfn", target, link])


# --- WARP ---

def provision_warp(ctx: Ctx) -> bool:
    """Install, register and bring up the WARP tunnel, then test it. False if
    WARP isn't usable (nothing on the client side depends on it yet)."""
    ctx.out.log("WARP tunnel")
    if not warp.install_wgcf(ctx, reinstall=ctx.args.force) or not warp.register(ctx, regenerate=ctx.args.force):
        return False
    profile = ctx.paths.wgcf_profile()
    changed = False
    if not os.path.isfile(profile):
        if not ctx.runner.dry_run:
            raise WgtError(f"missing {profile}")
        ctx.out.info(f"[dry-run] would write {ctx.cfg.warp_conf} from {profile} "
                     f"(Table = off, MTU {ctx.cfg.WARP_MTU}, no DNS)")
    else:
        ops.ensure_managed(ctx, ctx.cfg.warp_conf)
        changed = ctx.files.write_config(ctx.cfg.warp_conf, 0o600, warp.render_profile(ctx, profile))
    start_unit(ctx, ctx.cfg.WARP_IF, changed)
    guard_default_route(ctx)
    if ctx.runner.dry_run:
        ctx.out.log("Dry run: skipping WARP tests")
        return True
    ctx.out.log("WARP tests")
    res = checks.Tests(ctx).run("warp")
    if res.failed:
        ctx.out.warn(f"WARP failed {res.failed} check(s); see 'wgt status'")
        return False
    ctx.out.info(f"WARP is working ({res.passed} checks passed)")
    return True


# --- setup's questions and review ---

def ask_subnet(ctx: Ctx) -> bool:
    """First setup without --subnet: ask for the client subnet, Enter keeps the
    random default. --yes, --dry-run and no terminal take the default. True if
    something was typed."""
    if not ctx.cfg.subnet_defaulted or ctx.runner.dry_run or ctx.args.yes or not ui.have_tty():
        return False
    default = ctx.cfg.WG_SUBNET
    while True:
        reply = ui.ask(f"    Client subnet [{default}]: ")
        trial = copy.deepcopy(ctx.cfg)
        trial.values["WG_SUBNET"] = reply or default
        try:
            trial.finalize(ctx.facts.kernel_ipv6)
        except WgtError as e:
            ctx.out.info(str(e))
            continue
        ctx.cfg.values["WG_SUBNET"] = reply or default
        ctx.cfg.finalize(ctx.facts.kernel_ipv6)
        return bool(reply)


def ensure_subnet6(ctx: Ctx) -> None:
    """Pick the client IPv6 /64 once (saved with the rest)."""
    if not ctx.cfg.values.get("WG_SUBNET6"):
        ctx.cfg.values["WG_SUBNET6"] = net.random_subnet6()
        ctx.cfg.finalize(ctx.facts.kernel_ipv6)


def review_settings(ctx: Ctx, subnet_asked: bool) -> None:
    """Before anything changes: the settings this run uses, where each comes
    from and what differs from the last setup; then ask to continue (even with
    --yes, unless there's no terminal; --dry-run doesn't ask)."""
    fresh = not ctx.cfg.saved.get("WG_SUBNET")
    if not ctx.cfg.values.get("ENDPOINT"):
        ctx.cfg.values["ENDPOINT"] = live.trace_field(live.trace(ctx.runner, "-4"), "ip")
    ctx.out.log("Settings (first setup)" if fresh else f"Settings (re-run; saved in {ctx.paths.state})")
    rep = checks.settings_table(ctx, running=False, subnet_asked=subnet_asked)
    for line in rep.lines:
        ctx.out.line(line)
    if fresh:
        ctx.out.info("Nothing is set up yet; these are saved for re-runs and wgt client.")
    elif not rep.changes:
        ctx.out.info("No changes to settings since the last setup.")
    else:
        ctx.out.info("Changes since the last setup:")
        for c in rep.changes:
            ctx.out.info(f"  {c}")
        if rep.changes_client_configs and ctx.snap.clients:
            ctx.out.info("Client configs will be rewritten; re-send them.")
    ctx.runner.current_step = "review settings"
    if ctx.runner.dry_run:
        return
    if not ui.have_tty():
        if not ctx.args.yes:
            raise WgtError("no terminal to confirm the settings; re-run with --yes")
        return
    if not ui.yes("Continue?", default=True):
        raise WgtError("aborted at: review settings (nothing changed)")


def offer_hostname(ctx: Ctx) -> None:
    """Offer to rename the host to the server name when they differ (terminal
    only). /etc/hosts is updated and cloud-init kept from restoring the old name."""
    full = ctx.facts.hostname
    cur = full.split(".")[0]
    want = ctx.cfg.SERVER_NAME.lower()
    if cur.lower() == want or not re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", want):
        return
    if ctx.runner.dry_run or ctx.args.yes or not ui.have_tty():
        return
    if not ui.yes(f"Update hostname {cur} to {want}?", default=True):
        return
    new = want + (full[len(cur):] if "." in full else "")   # keep the domain part
    ctx.runner.step(f"Rename host {full} -> {new} (and in {ctx.paths.hosts_file})",
                    f"hostnamectl set-hostname {full}; put {cur} back in {ctx.paths.hosts_file}; "
                    f"rm {os.path.join(ctx.paths.cloud_cfg_dir, '99-wg-tools-hostname.cfg')}")
    ctx.runner.run(["hostnamectl", "set-hostname", new])
    hosts = store.read_text(ctx.paths.hosts_file) or ""
    if re.search(rf"\b{re.escape(cur)}\b", hosts):
        ctx.files.write(ctx.paths.hosts_file, 0o644, re.sub(rf"\b{re.escape(cur)}\b", want, hosts))
    if os.path.isdir(ctx.paths.cloud_cfg_dir):
        ctx.files.write(os.path.join(ctx.paths.cloud_cfg_dir, "99-wg-tools-hostname.cfg"), 0o644,
                        "preserve_hostname: true\n")


def renumber_clients(ctx: Ctx) -> None:
    """A new --subnet with clients present: move each client to the same host
    number in the new subnet (keys unchanged) and rewrite allowed-IPs entries
    naming the old subnet. Their configs are updated afterwards; re-send them."""
    old = ctx.cfg.saved.get("WG_SUBNET", "")
    if not old or old == ctx.cfg.WG_SUBNET or not ctx.snap.clients:
        return
    old_int = net.network_int(old)
    moves, entries = [], []
    for c in ctx.snap.clients:
        n = ctx.cfg.net_int + net.ip2int(c.ip) - old_int
        if not ctx.cfg.first_host <= n <= ctx.cfg.last_host:
            raise WgtError(f"client '{c.name}' ({c.ip}) has no matching address in {ctx.cfg.WG_SUBNET}; "
                           f"use a subnet at least as large as {old}")
        new_ip = net.int2ip(n)
        allowed = ",".join(ctx.cfg.WG_SUBNET if a == old else a for a in c.allowed.split(",")) if c.allowed else ""
        moves.append(f"{c.name} {c.ip} -> {new_ip}")
        entries.append(store.Client(c.name, new_ip, c.pub, allowed))
    ctx.runner.step(f"Move {len(moves)} client(s) from {old} to {ctx.cfg.WG_SUBNET} (same host numbers and keys; "
                    "their configs change, re-send them)", f"wgt setup --subnet {old}")
    for m in moves:
        ctx.out.info(m)
    ops.confirm(ctx, f"Move the clients to {ctx.cfg.WG_SUBNET}?")
    ctx.files.write(ctx.paths.client_db, 0o600, store.format_clients(e.fields_line() for e in entries))
    ctx.reload()


def save_config(ctx: Ctx) -> None:
    """Persist the settings for later runs."""
    for k in CONFIG_KEYS:
        if store.state_get(ctx.snap.state, k) != ctx.cfg.values[k]:
            ops.state_set(ctx, k, ctx.cfg.values[k])


def record_baseline(ctx: Ctx) -> None:
    """Record the pre-install default route, public interface and forwarding once."""
    route = ctx.facts.default_route
    if not route:
        raise WgtError("no IPv4 default route; refusing to continue")
    if host.route_dev(route) == ctx.cfg.WARP_IF:
        raise WgtError(f"default route already points at {ctx.cfg.WARP_IF}; fix routing first")
    if not store.state_get(ctx.snap.state, "DEFAULT_ROUTE"):
        ops.state_set(ctx, "DEFAULT_ROUTE", route)
        ops.state_set(ctx, "PUB_IF", host.route_dev(route))
        ops.state_set(ctx, "ORIG_IP_FORWARD", live.sysctl(ctx.runner, "net.ipv4.ip_forward"))
    # Recorded separately: servers set up before IPv6 support lack it.
    if not store.state_get(ctx.snap.state, "ORIG_IP6_FORWARD") and ctx.facts.kernel_ipv6:
        ops.state_set(ctx, "ORIG_IP6_FORWARD", live.sysctl(ctx.runner, "net.ipv6.conf.all.forwarding"))
    ctx.out.info(f"default route: {route} (public interface {host.route_dev(route)})")


def ensure_server_keys(ctx: Ctx) -> bool:
    """Create the server keypair unless it exists (--new-server-keys regenerates
    it: every client then has to be rotated). True if new."""
    p = ctx.paths
    have = all(os.path.isfile(f) and os.path.getsize(f) > 0 for f in (p.server_key, p.server_pub))
    if have and not ctx.args.new_server_keys:
        ctx.out.info("server keys present")
        return False
    if os.path.isfile(p.server_key) and os.path.getsize(p.server_key) > 0:
        ctx.runner.step("Regenerate server keys (--new-server-keys): every client must be re-issued "
                        "(wgt client rotate)",
                        f"restore {p.server_key} and {os.path.basename(p.server_pub)} from their copies in "
                        f"{ctx.files.change_dir(p.server_key)}")
        ops.confirm(ctx, "Regenerate the server keys? Every client stops working until rotated")
        ctx.files.backup(p.server_key)   # the old keys stay recoverable until --purge
        ctx.files.backup(p.server_pub)
    else:
        ctx.runner.step(f"Generate server keypair ({p.server_key})", f"rm {p.server_key} {p.server_pub}")
    if ctx.runner.dry_run:
        return True
    key = ctx.runner.query(["wg", "genkey"]).strip()
    pub = ctx.runner.query(["wg", "pubkey"], input=key + "\n").strip()
    if not key or not pub:
        raise WgtError("could not generate the server keys")
    write_private(p.server_key, key + "\n")
    write_private(p.server_pub, pub + "\n")
    return True


def sync_client_configs(ctx: Ctx, old_name: str) -> None:
    """Keep every client config named <server>.<client>.conf (renaming ones from
    an older server name or from before server names), pointed at the current
    endpoint, port, DNS and MTU, with its current addresses and allowed IPs.
    Keys are unchanged, so only the configs need re-sending."""
    # Patched line by line rather than re-rendered: re-rendering needs each
    # client's private key back out of its file, and would rewrite configs the
    # bash version wrote (and every device would need its config re-sent).
    endpoint = f"{ctx.cfg.values['ENDPOINT']}:{ctx.cfg.WG_PORT}"
    stale: List[str] = []
    for c in ctx.snap.clients:
        conf = ctx.paths.client_conf(ctx.cfg.SERVER_NAME, c.name)
        src = conf if os.path.isfile(conf) else _move_to_current_name(ctx, c.name, conf, old_name)
        if not src:
            continue
        text = store.read_text(src) or ""
        # Address keeps the bash version's "ip/32, ip6/128" spacing (only the first comma).
        want = {"Endpoint": endpoint, "Address": ctx.cfg.client_addrs(c.ip).replace(",", ", ", 1),
                "DNS": ctx.cfg.WG_DNS, "MTU": ctx.cfg.WG_MTU}
        if c.allowed:
            want["AllowedIPs"] = c.allowed.replace(",", ", ")
        new = text
        for key, value in want.items():
            new = re.sub(rf"(?m)^{key} = .*$", f"{key} = {value}".replace("\\", "\\\\"), new)
        if new != text:
            ctx.files.write(conf, 0o600, new)
        if (new != text or src != conf) and c.name not in stale:
            stale.append(c.name)
    if stale:
        ctx.out.warn(f"client configs changed (now {ctx.cfg.SERVER_NAME}.<client>.conf at {endpoint}); re-send them "
                     f"(wgt client share/show --qr): {' '.join(stale)}")


def _move_to_current_name(ctx: Ctx, name: str, conf: str, old_name: str) -> str:
    """A client config under an older name (the server's old name, or none):
    move it to <conf>. Returns where its text is now ("" if there is none; in
    a dry run it is still at the old name)."""
    olds = ([ctx.paths.client_conf(old_name, name)] if old_name else []) + [ctx.paths.client_conf("", name)]
    src = next((o for o in olds if os.path.isfile(o)), "")
    if not src:
        return ""
    ctx.runner.step(f"Rename {src} to {os.path.basename(conf)}", f"mv {conf} {src}")
    if ctx.runner.dry_run:
        return src
    os.replace(src, conf)
    return conf


def write_sysctl(ctx: Ctx) -> None:
    """Persist and apply the forwarding sysctls."""
    pub = ctx.pub_if
    accept_ra = live.sysctl(ctx.runner, f"net.ipv6.conf.{pub}.accept_ra") or None
    lines = render.sysctl_lines(ctx.cfg, pub, ctx.facts.has_ipv6, accept_ra)
    # Remember accept_ra before we change it, so rollback can restore it.
    if any("accept_ra" in line for line in lines) and not store.state_get(ctx.snap.state, "ORIG_ACCEPT_RA"):
        ops.state_set(ctx, "ORIG_ACCEPT_RA", accept_ra or "")
    current = store.read_text(ctx.paths.sysctl_file)
    if current is None or [line for line in current.splitlines() if not line.startswith("#")] != lines:
        orig = store.state_get(ctx.snap.state, "ORIG_IP_FORWARD") or live.sysctl(ctx.runner, "net.ipv4.ip_forward")
        ctx.runner.step(f"Enable forwarding ({ctx.paths.sysctl_file}: {'; '.join(lines)})",
                        f"wgt rollback (restores ip_forward={orig})")
        ops.confirm(ctx, "Enable forwarding?", default_yes=True)   # clients get nowhere without it
    ctx.files.write(ctx.paths.sysctl_file, 0o644, render.render_sysctl(lines))
    for line in lines:
        k, v = (x.strip() for x in line.split("="))
        if live.sysctl(ctx.runner, k) != v:
            ctx.runner.run(["sysctl", "-q", "-w", f"{k}={v}"])


def write_dropin(ctx: Ctx) -> None:
    """Start wg0 after warp at boot (Wants, not Requires: with WARP off, wg0 must
    come up even if warp can't; with it on, RT_TABLE's blackhole fails closed)."""
    if ctx.files.write(ctx.cfg.dropin, 0o644, render.render_dropin(ctx.cfg)):
        ctx.runner.run(["systemctl", "daemon-reload"])


def stop_before_rewrite(ctx: Ctx) -> bool:
    """wg0's config is about to change: stop wg0 with its current config first,
    so its PostDown removes exactly the rules its PostUp added (old subnet, old
    port). True if it was stopped."""
    unit = f"wg-quick@{ctx.cfg.WG_IF}"
    current = ctx.files.read(ctx.cfg.wg_conf)
    if not ops.wg_up(ctx) or current is None or settings_of(current) == settings_of(ops.render_wg_conf(ctx)):
        return False   # down, new, or only comments differ: no restart needed
    ctx.runner.step(f"Restart {ctx.cfg.WG_IF} for its new config (clients reconnect in a few seconds)",
                    f"restore the .bak in {ctx.paths.change_dir}, then systemctl restart {unit}")
    ops.confirm(ctx, f"Restart {ctx.cfg.WG_IF} now?")
    if live.unit_active(ctx.runner, unit):
        ctx.runner.run(["systemctl", "stop", unit])
    else:
        ctx.runner.run(["wg-quick", "down", ctx.cfg.WG_IF])
    return True


# --- commands ---

def cmd_setup(ctx: Ctx, words: List[str]) -> None:
    """wgt setup: install or update everything; safe to re-run, and a change
    of settings is shown and confirmed first."""
    ops.no_more_words(words, "wgt setup [--name N] [--subnet ...] (settings are flags)")
    ops.take_lock(ctx)
    first = not ops.is_set_up(ctx)              # before save_config records it
    old_name = ctx.cfg.saved.get("SERVER_NAME", "")
    asked = ask_subnet(ctx)
    ensure_subnet6(ctx)
    review_settings(ctx, asked)
    # Refuse to take over a wg0.conf we didn't create, before saving or touching anything.
    ops.ensure_managed(ctx, ctx.cfg.wg_conf)
    renumber_clients(ctx)
    offer_hostname(ctx)
    save_config(ctx)
    ops.state_unset(ctx, "EXIT")   # older versions' mode key; WARP replaces it
    record_baseline(ctx)
    install_packages(ctx)
    link_wgt(ctx)
    if not provision_warp(ctx):
        if first and ctx.cfg.warp_on:
            ctx.out.warn("WARP isn't usable here; finishing with WARP off (clients exit via this server's IP). "
                         "Fix it, then: wgt warp on")
            ctx.cfg.values["WARP"] = "off"
            save_config(ctx)
        elif ctx.cfg.warp_on:
            ctx.out.warn("WARP is on but not working: clients are blocked until it is (or: wgt warp off)")
    ctx.out.log(f"WireGuard server (WARP {ctx.cfg.WARP}: client traffic leaves via {ops.default_out_if(ctx)})")
    keys_changed = ensure_server_keys(ctx)
    ops.resolve_endpoint(ctx, save=True)
    sync_client_configs(ctx, old_name)
    write_sysctl(ctx)
    write_dropin(ctx)
    stopped = stop_before_rewrite(ctx)
    changed = ops.write_wg_conf(ctx) or keys_changed
    units.open_firewall(ctx, rules.out_ifs(ctx.cfg, ctx.pub_if, ctx.snap.exits))
    if stopped and ctx.runner.dry_run:
        ctx.out.info(f"[dry-run] would then start wg-quick@{ctx.cfg.WG_IF} with the new config")
    else:
        try:
            start_unit(ctx, ctx.cfg.WG_IF, changed)
        except BaseException:
            # Only if wg0 didn't come up: answering no to a restart leaves a
            # running wg0 (and its rules) alone.
            if not ops.wg_up(ctx):
                ctx.out.error(f"removing partially applied {ctx.cfg.WG_IF} routing/NAT rules")
                run_hooks(ctx, up=False)
            raise
    guard_default_route(ctx)
    units.write_watchdog(ctx)
    exits.sync(ctx)
    ctx.out.line()
    ctx.out.info(f"WARP is {ctx.cfg.WARP}; switch with: wgt warp on|off")
    ctx.out.info(f"REMINDER: open UDP {ctx.cfg.WG_PORT} in your VPS provider's firewall / security group.")
    ctx.out.info("Next: wgt client add <name>")


def cmd_warp(ctx: Ctx, words: List[str]) -> None:
    """Show or switch WARP: rewrites wg0.conf (for the next start) and adds or
    removes the policy rule live; wg0 isn't restarted."""
    ops.require_setup(ctx)
    ops.no_more_words(words[1:], "wgt warp [on|off]")
    want = words[0] if words else ""

    if not want:
        ctx.out.info(f"WARP is {ctx.cfg.WARP}: client traffic leaves via {ops.default_out_if(ctx)}")
        return
    if want not in ("on", "off"):
        raise WgtError("usage: wgt warp [on|off]")
    ops.take_lock(ctx)
    if want == "on" and not provision_warp(ctx):
        raise WgtError("WARP isn't working; leaving it off (see 'wgt status')")
    conf = store.read_text(ctx.cfg.wg_conf) or ""
    if want == ctx.cfg.WARP and f"(WARP {want})" in conf:
        ctx.out.info(f"WARP is already {want} (client traffic leaves via {ops.default_out_if(ctx, want)})")
        if ops.wg_up(ctx):
            ctx.runner.sh_lines(rules.warp_rule_cmds(ctx.cfg, want == "on"))
        return
    ctx.cfg.values["WARP"] = want
    other = "off" if want == "on" else "on"
    ctx.runner.step(f"Switch WARP {want}: client traffic leaves via {ops.default_out_if(ctx, want)}",
                    f"wgt warp {other}")
    save_config(ctx)
    ops.write_wg_conf(ctx)
    if ops.wg_up(ctx):
        ctx.runner.sh_lines(rules.warp_rule_cmds(ctx.cfg, want == "on"))
        ctx.out.info("applied live; clients stay connected (open connections reset once)")
    else:
        ctx.out.info(f"{ctx.cfg.WG_IF} is down; applies when it starts")


def cmd_rollback(ctx: Ctx, words: List[str]) -> None:
    """Undo setup: stop everything, remove every rule, restore original files;
    --purge also deletes keys, clients, WARP accounts and exit cities."""
    ops.no_more_words(words, "wgt rollback [--purge]")
    ops.take_lock(ctx)
    cfg = ctx.cfg
    ctx.runner.step(f"Roll back: stop {cfg.WG_IF}, {cfg.WARP_IF} and exit cities, remove routing/NAT/firewall "
                    "rules, restore original files", "wgt setup")
    if ctx.args.purge:
        ctx.out.warn("--purge also deletes the WARP accounts, server and client keys, client configs and "
                     "exit cities (irreversible)")
    ops.confirm(ctx, "Proceed with rollback?")
    # Read before anything below changes the state file.
    state = ctx.snap.state
    _stop_everything(ctx)
    _remove_rules(ctx)
    _remove_units(ctx)
    _restore_forwarding(ctx, state)
    configs = [cfg.wg_conf, cfg.warp_conf]
    configs += [path for e in ctx.snap.exits for path in (ctx.paths.px_conf(e.code), ctx.paths.wx_conf(e.code))]
    for path in configs:
        ctx.files.restore_original(path, remove_new=ctx.args.purge)
    if ctx.args.purge:
        _purge(ctx)
    else:
        _keep(ctx)
    ctx.out.log("Rollback done; the main routing table was never changed")


def _stop_everything(ctx: Ctx) -> None:
    """The watchdogs first (they'd bring things back), then exit cities, wg0, warp."""
    r, cfg = ctx.runner, ctx.cfg
    timers = [f"{name}.timer" for name in (WATCHDOG, EXIT_WATCHDOG)
              if os.path.exists(ctx.paths.unit_file(f"{name}.timer"))]
    if timers:
        r.run(["systemctl", "disable", "--now", *timers], check=False)
    for e in ctx.snap.exits:
        exits.down(ctx, e)
    for ifc in (cfg.WG_IF, cfg.WARP_IF):
        r.run(["systemctl", "disable", "--now", f"wg-quick@{ifc}"], check=False)
    for ifc in (cfg.WG_IF, cfg.WARP_IF):
        if live.if_exists(r, ifc):
            r.run(["ip", "link", "del", "dev", ifc])


def _remove_rules(ctx: Ctx) -> None:
    """wg0's PostDown (routing, NAT, firewall) run directly, and the ufw rules setup added."""
    run_hooks(ctx, up=False)
    if store.state_get(ctx.snap.state, "UFW") == "1":
        units.close_firewall(ctx, rules.out_ifs(ctx.cfg, ctx.pub_if, ctx.snap.exits), port=True)
        ops.state_unset(ctx, "UFW")


def _remove_units(ctx: Ctx) -> None:
    """wg0's boot-order drop-in and both watchdog timers."""
    cfg, files = ctx.cfg, ctx.files
    # Our own drop-in's copies would keep its directory from being removed.
    if store.backup_orig(store.read_text(ctx.paths.backup_map), cfg.dropin) == "NEW":
        files.remove(*glob.glob(cfg.dropin + ".*.bak"))
    files.restore_original(cfg.dropin, remove_new=True)
    files.remove_empty_dir(cfg.dropin_dir)
    units.remove_timer(ctx, WATCHDOG)
    units.remove_timer(ctx, EXIT_WATCHDOG)
    files.remove(ctx.paths.warp_marker)
    ctx.runner.run(["systemctl", "daemon-reload"])


def _restore_forwarding(ctx: Ctx, state: Optional[str]) -> None:
    """The sysctl file and the forwarding values recorded before setup."""
    r = ctx.runner
    ctx.files.restore_original(ctx.paths.sysctl_file, remove_new=True)
    fwd, fwd6, ra = (store.state_get(state, k) for k in ("ORIG_IP_FORWARD", "ORIG_IP6_FORWARD", "ORIG_ACCEPT_RA"))
    if fwd:
        r.run(["sysctl", "-q", "-w", f"net.ipv4.ip_forward={fwd}"])
    if fwd6:
        r.run(["sysctl", "-q", "-w", f"net.ipv6.conf.all.forwarding={fwd6}"])
    if ra:
        r.run(["sysctl", "-q", "-w", f"net.ipv6.conf.{host.pub_if(state, ctx.facts)}.accept_ra={ra}"], check=False)


def _purge(ctx: Ctx) -> None:
    """--purge: delete everything setup made (originals were restored already)."""
    cfg, files, p = ctx.cfg, ctx.files, ctx.paths
    for path in (cfg.wg_conf, cfg.warp_conf, p.server_key, p.server_pub, p.client_db, p.sysctl_file):
        files.remove(*glob.glob(path + ".*.bak"))   # copies left by older versions
    # The change copies hold old keys.
    files.remove_tree(p.change_dir)
    files.remove_empty_dir(p.backup_dir)
    # Configs carrying our marker are ours (a restored original never does).
    for path in (cfg.wg_conf, cfg.warp_conf):
        text = store.read_text(path)
        if text is not None and MARKER in text:
            files.remove(path)
    files.remove(p.server_key, p.server_pub, p.client_db, p.disabled_db, p.state, p.backup_map, p.exit_db)
    for d in (p.wgcf_dir, p.client_dir, p.psk_dir, p.exit_dir):
        files.remove_tree(d)
    files.remove(p.wgt_link)
    ctx.out.info(f"left installed: packages and {p.wgcf_bin}")


def _keep(ctx: Ctx) -> None:
    """Without --purge: mark the server not set up, keep keys, clients and accounts."""
    p = ctx.paths
    # Not set up any more (wgt client and restore check this).
    ops.state_unset(ctx, "WARP")
    ops.state_unset(ctx, "EXIT")
    kept = f"keys, clients ({p.client_db}, {p.client_dir})"
    if os.path.isdir(p.wgcf_dir):
        kept += f", WARP account ({p.wgcf_dir})"
    if ctx.snap.exits:
        kept += f", exit cities ({p.exit_dir})"
    ctx.out.info(f"kept: {kept} (rollback --purge removes them)")
