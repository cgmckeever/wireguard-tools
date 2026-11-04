"""`wgt client ...`: the clients of this server. Each has a keypair, a
preshared key and an address; its config is
/etc/wireguard/clients/<server>.<name>.conf. A client named <code>.<name>
exits through jump <code>; one in clients.nowarp has WARP off (it
leaves straight out of its exit: this server's IP, or its jump's
tunnel). Changes apply live; other clients stay connected."""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING, List, Optional

from . import live, net, ops, render, rules, store, system, ui
from .const import MIB
from .errors import WgtError

if TYPE_CHECKING:
    from .ctx import Ctx

# --- helpers ---

def client_conf(ctx: Ctx, name: str) -> str:
    """Where client <name>'s config lives: <server>.<name>.conf."""
    return ctx.paths.client_conf(ctx.cfg.SERVER_NAME, name)


def valid_name(name: str) -> None:
    """Raise if <name> isn't a valid client name (the rule: store.valid_client_name)."""
    why = store.valid_client_name(name)
    if why:
        raise WgtError(why)


def allowed_list(value: str) -> str:
    """--allowed-ips as a comma list without spaces."""
    return re.sub(r"\s", "", value)


def next_free_ip(ctx: Ctx, planned: List[str]) -> str:
    """First unused host address (the server takes .1). Addresses handed out
    earlier in this run count as used (a dry run writes nothing)."""
    used = {ctx.cfg.server_ip, *(c.ip for c in ctx.snap.clients), *planned}
    for n in range(ctx.cfg.first_host, ctx.cfg.last_host + 1):
        ip = net.int2ip(n)
        if ip not in used:
            planned.append(ip)
            return ip
    raise WgtError(f"no free addresses left in {ctx.cfg.WG_SUBNET}")


def pick_client(ctx: Ctx, verb: str) -> str:
    """No name given: a numbered list of clients to pick from (needs a terminal)."""
    clients = sorted(ctx.snap.clients, key=lambda c: store.device_key(c.name))
    if not clients:
        raise WgtError("no clients yet (add one: wgt client add <name>)")
    if not ui.have_tty() or ctx.args.yes:
        raise WgtError(f"usage: wgt client {verb} <name>")
    try:
        i = ui.menu("", [f"{store.device_of(c.name):<20} {exit_label(ctx, c.name):<16} {c.ip}" for c in clients],
                    f"{verb.capitalize()} which client? (number or name)")
    except ValueError as e:
        if ctx.snap.client(str(e)):
            return str(e)
        raise WgtError(f"no client '{e}'") from None
    if i is None:
        raise WgtError("no client chosen")
    return clients[i].name


def pick_exits(ctx: Ctx, name: str) -> List[str]:
    """Ask which exit(s) a new client gets: codes, "host" (this server) being the default."""
    codes = [e.code for e in ctx.snap.exits]
    items = [f"{'host':<6} {ctx.cfg.SERVER_NAME} itself (default)"]
    items += [f"{e.code:<6} {e.label}" for e in ctx.snap.exits]
    ui.say("\n".join(["", f"  Exit for {name}:"] + [f"    {i + 1}) {it}" for i, it in enumerate(items)]) + "\n")
    reply = ui.ask("  Exit [Enter = 1 (host); several: 1,2]: ")
    if not reply:
        return ["host"]
    out = []
    for part in reply.replace(",", " ").split():
        if not part.isdigit():
            out.append(part)
        elif 1 <= int(part) <= len(items):
            out.append("host" if int(part) == 1 else codes[int(part) - 2])
        else:
            raise WgtError(f"no exit number {part}")
    if not out:
        raise WgtError("no exit chosen")
    return out


def fetch_hint(ctx: Ctx, conf: str) -> None:
    """How to copy a config to your own machine (WGT_SSH_TARGET, set by wgt-ctl,
    is the login that machine uses for this server)."""
    ep = (ctx.cfg.values.get("ENDPOINT") or store.state_get(ctx.snap.state, "ENDPOINT")).strip("[]")
    target = os.environ.get("WGT_SSH_TARGET") or f"root@{ep or '<server>'}"
    ctx.out.info(f"fetch it with: scp {target}:{conf} .")


def qr_data(text: str) -> str:
    """Config lines that matter, without comments or padding, for a smaller QR code."""
    out = []
    for line in text.splitlines():
        if re.match(r"^\s*(#|$)", line):
            continue
        line = re.sub(r"\s*=\s*", "=", line, count=1)
        out.append(re.sub(r",\s+", ",", line))
    return "\n".join(out) + "\n"


def show_qr(ctx: Ctx, conf: str) -> None:
    """The config as a terminal QR code (it holds the private key). In iTerm2 or
    WezTerm it is drawn as a QR_WIDTH-cell-wide image, streamed so the key
    never touches disk; QR_WIDTH=0 forces text."""
    width = os.environ.get("QR_WIDTH", "32")
    if not shutil.which("qrencode"):
        raise WgtError("qrencode not installed")
    if not width.isdigit():
        raise WgtError(f"QR_WIDTH must be a number of terminal columns: '{width}'")
    ctx.out.warn("this QR code contains the client's private key")
    data = qr_data(store.read_text(conf) or "")
    term = f"{os.environ.get('LC_TERMINAL', '')} {os.environ.get('TERM_PROGRAM', '')}"
    inline = re.search(r"iTerm2|iTerm\.app|WezTerm", term) and not os.environ.get("TMUX")
    if int(width) > 0 and sys.stdout.isatty() and inline:
        p = subprocess.run(["qrencode", "-t", "PNG", "-s", "4", "-m", "2", "-o", "-"], input=data.encode(),
                           stdout=subprocess.PIPE)
        if p.returncode != 0:
            raise WgtError("qrencode failed")
        png = p.stdout
        sys.stdout.write(f"\033]1337;File=inline=1;width={width};preserveAspectRatio=1:"
                         f"{base64.b64encode(png).decode()}\a\n")
        sys.stdout.flush()
    else:
        sys.stdout.write(ctx.runner.query(["qrencode", "-t", "ansiutf8", "-m", "1"], input=data))


def issue_client(ctx: Ctx, name: str, ip: str, allowed: str, endpoint: str, old_pub: str = "") -> None:
    """Generate keys for <name> at <ip>, write its config (pointing at
    <endpoint>, from ops.resolve_endpoint), update clients.db, wg0.conf and the
    live interface (<old_pub> is replaced). A jump client's routing rules
    go in before its peer, so its first packets can't leave through the
    default exit."""
    if not endpoint:
        raise WgtError("no client endpoint (pass --endpoint <ip-or-host>)")
    conf = client_conf(ctx, name)
    if ctx.runner.dry_run:
        ctx.out.info(f"[dry-run] would generate a keypair + preshared key, write {conf} (mode 600),")
        ctx.out.info(f"[dry-run] and add the peer to {ctx.cfg.wg_conf} and live {ctx.cfg.WG_IF} "
                     f"(AllowedIPs {allowed.replace(',', ', ')})")
        return
    os.makedirs(ctx.paths.client_dir, mode=0o700, exist_ok=True)
    os.makedirs(ctx.paths.psk_dir, mode=0o700, exist_ok=True)
    priv = ctx.runner.query(["wg", "genkey"]).strip()
    pub = ctx.runner.query(["wg", "pubkey"], input=priv + "\n").strip()
    psk = ctx.runner.query(["wg", "genpsk"]).strip()
    if not (priv and pub and psk):
        raise WgtError("could not generate keys (is wireguard-tools installed?)")
    server_pub = (store.read_text(ctx.paths.server_pub) or "").strip()
    # Render first: a template error must stop us before anything is written.
    content = render.render_client_conf(ctx.cfg, ctx.snap, name, ip, priv, server_pub, psk,
                                        endpoint, allowed)
    ctx.files.write(ctx.paths.psk(name), 0o600, psk + "\n")
    ctx.files.write(conf, 0o600, content)
    entry = store.Client(name, ip, pub, allowed)
    ops.save_client(ctx, entry)
    ops.write_wg_conf(ctx)
    apply_peer(ctx, entry, old_pub)
    hand_over(ctx, conf)


def apply_peer(ctx: Ctx, entry: store.Client, old_pub: str) -> None:
    """Make the live wg0 match a new or re-keyed client (wg0.conf is already
    written): its exit rules first, then its peer, replacing <old_pub>. A
    disabled client gets no peer."""
    up = ops.wg_up(ctx)
    disabled = ctx.snap.is_disabled(entry.name)
    if up:
        ctx.runner.sh_lines(rules.client_rule_cmds(ctx.cfg, ctx.snap, entry, up=True))
        if old_pub and (old_pub != entry.pub or disabled):
            ctx.runner.run(["wg", "set", ctx.cfg.WG_IF, "peer", old_pub, "remove"])
    if disabled:
        ctx.out.info(f"'{entry.name}' is disabled; the new config works once enabled "
                     f"(wgt client enable {entry.name})")
    elif up:
        ops.add_live_peer(ctx, entry.name, entry.pub, entry.ip)
    else:
        ctx.out.warn(f"{ctx.cfg.WG_IF} is not up; the peer is in {ctx.cfg.wg_conf} and loads when it starts")


def hand_over(ctx: Ctx, conf: str) -> None:
    """Show the new config's QR code (by default when a person is watching;
    --qr forces it, --no-qr skips it) and how to fetch the file."""
    qr = ctx.args.qr
    if qr or (qr is None and sys.stdout.isatty()):
        if qr or shutil.which("qrencode"):
            show_qr(ctx, conf)
        else:
            ctx.out.warn("qrencode not installed; no QR code")
    ctx.out.line()
    ctx.out.info(f"client config: {conf}")
    fetch_hint(ctx, conf)


# --- the clients table (client list, status) ---

def client_rows(ctx: Ctx) -> List[ui.ClientRow]:
    """A table row per client, in clients.db order."""
    hs = live.handshakes(ctx.runner, ctx.cfg.WG_IF)
    tx = live.transfers(ctx.runner, ctx.cfg.WG_IF)
    now = ctx.clock()
    rows = []
    for cl in ctx.snap.clients:
        seen = "disabled" if ctx.snap.is_disabled(cl.name) else ui.ago(hs.get(cl.pub, 0), now)
        rx_b, tx_b = tx.get(cl.pub, (0, 0))
        rows.append(ui.ClientRow(cl.name, exit_short(ctx, cl.name), cl.ip, seen,
                                 f"{rx_b // MIB}/{tx_b // MIB}", cl.allowed.replace(",", ", "),
                                 exit_label(ctx, cl.name)))
    return rows


def exit_short(ctx: Ctx, name: str, warp_off: Optional[bool] = None) -> str:
    """Where a client exits, as a code: its jump's code (<code>/direct
    with WARP off), or warp/direct (no jump: the server's WARP switch, or
    direct with WARP off)."""
    code = store.client_exit(name)
    off = ctx.snap.warp_off(name) if warp_off is None else warp_off
    if code:
        return f"{code}/direct" if off else code
    return "warp" if ctx.cfg.warp_on and not off else "direct"


def exit_label(ctx: Ctx, name: str) -> str:
    """Where a client exits, for people: its jump's full name (the label
    given at exit add, e.g. "Paris, France"; "(no WARP)" added with WARP off),
    or warp/direct (no jump)."""
    code = store.client_exit(name)
    e = ctx.snap.exit(code) if code else None
    if e is None:
        return exit_short(ctx, name)
    return (e.label or code) + (" (no WARP)" if ctx.snap.warp_off(name) else "")


def print_clients(ctx: Ctx, prefix: str = "", numbered: bool = True) -> List[str]:
    """Print the clients table, device first (the exit is its own column);
    returns the full client names in the order shown."""
    number = f"{'#':>3} " if numbered else ""
    ctx.out.line(prefix + number + ui.CLIENT_FMT.format(*ui.CLIENT_HEAD))
    rows = sorted(client_rows(ctx), key=lambda row: store.device_key(row.name))
    for i, row in enumerate(rows, 1):
        number = f"{i:>3} " if numbered else ""
        ctx.out.line(prefix + number + ui.CLIENT_FMT.format(*row.cells()))
    return [row.name for row in rows]


# --- commands ---

def cmd_add(ctx: Ctx, words: List[str]) -> None:
    """add <name> [--exit host|<code>[,...]] [--warp on|off]: one client per
    exit (host = this server, the default), named <name> and <code>.<name>;
    asks when jumps exist. --warp off: they leave without our WARP."""
    usage = "wgt client add <name> [--exit host|<code>[,...]] [--warp on|off]"
    if not words:
        raise WgtError(f"usage: {usage}")
    ops.no_more_words(words[1:], usage)
    name = words[0]
    valid_name(name)
    base = name.split(".", 1)[1] if "." in name else name
    exit_codes = ctx.args.exit_codes
    if "." in name:
        if exit_codes:
            raise WgtError(f"give the exit either in the name ({name}) or with --exit, not both")
        exit_codes = store.client_exit(name)
    ops.take_lock(ctx)
    ops.require_server(ctx)
    if exit_codes:
        codes = exit_codes.replace(",", " ").split()
    elif ctx.snap.exits and not ctx.args.yes and not ctx.runner.dry_run and ui.have_tty():
        codes = pick_exits(ctx, base)
    else:
        codes = ["host"]
        if not ctx.snap.exits:
            ctx.out.info(f"no jumps on this server: the client exits via {exit_short(ctx, base)} "
                         "(add one: wgt jump add <code> <wg.conf>)")
    uniq: List[str] = []
    for code in codes:
        code = "host" if code in store.HOST_EXITS else code
        if code != "host" and ctx.snap.exit(code) is None:
            raise WgtError(f"no jump '{code}' on this server (see: wgt jump list)")
        if code not in uniq:
            uniq.append(code)
    endpoint = ops.resolve_endpoint(ctx, save=False)
    planned: List[str] = []
    for code in uniq:
        add_one(ctx, base if code == "host" else f"{code}.{base}", planned, endpoint, ctx.args.warp == "off")


def add_one(ctx: Ctx, name: str, planned: List[str], endpoint: str, warp_off: bool = False) -> None:
    """Add one client (skipped if it exists). <warp_off> is recorded before its
    rules and peer go in, so its first packets already take the right way out."""
    existing = ctx.snap.client(name)
    if existing:
        ctx.out.info(f"client '{name}' already exists ({existing.ip}); config: {client_conf(ctx, name)}")
        ctx.out.info(f"use 'wgt client rotate {name}' to issue new keys"
                     + (f", 'wgt client warp {name} on|off' to switch its WARP" if ctx.args.warp else ""))
        return
    ip = next_free_ip(ctx, planned)
    ctx.runner.step(f"Add client '{name}' at {ip} (exit: {exit_short(ctx, name, warp_off)})",
                    f"wgt client remove {name}")
    ops.set_nowarp(ctx, name, warp_off)
    issue_client(ctx, name, ip, allowed_list(ctx.cfg.CLIENT_ALLOWED_IPS), endpoint)
    tunnel = f"{ctx.cfg.SERVER_NAME}.{name}"
    if len(tunnel) > 15:
        ctx.out.info(f"note: Android's WireGuard app takes tunnel names up to 15 characters; '{tunnel}' is "
                     f"{len(tunnel)}, so shorten it when importing there")


def get_name(ctx: Ctx, words: List[str], verb: str) -> str:
    """The one client name a command takes, or the picker if none was given."""
    ops.no_more_words(words[1:], f"wgt client {verb} <name>")
    name = words[0] if words else pick_client(ctx, verb)
    valid_name(name)
    return name


def cmd_rotate(ctx: Ctx, words: List[str]) -> None:
    """New keys for an existing client: same address and exit, old config stops working."""
    name = get_name(ctx, words, "rotate")
    ops.take_lock(ctx)
    ops.require_server(ctx)
    c = ctx.snap.client(name)
    if c is None:
        raise WgtError(f"no client '{name}' (see: wgt client list)")
    # Keep the client's own AllowedIPs unless --allowed-ips was given.
    allowed = allowed_list(ctx.cfg.CLIENT_ALLOWED_IPS)
    if ctx.cfg.sources.get("CLIENT_ALLOWED_IPS") not in ("flag", "env") and c.allowed:
        allowed = c.allowed
    endpoint = ops.resolve_endpoint(ctx, save=False)
    ctx.runner.step(f"Rotate keys for '{name}' ({c.ip}); its current config stops working",
                    "none (old keys are discarded)")
    ops.confirm(ctx, f"Rotate keys for {name}?")
    issue_client(ctx, name, c.ip, allowed, endpoint, c.pub)


def removal_targets(name: str, directory: str) -> List[str]:
    """Which files in <directory> belong to client <name>: <server>.<name>.conf
    (under any server name: the server may have been renamed) and its change
    copies. The part before ".<name>.conf" must be a server name (no dot), so
    removing "laptop" leaves "<server>.par-fr.laptop.conf"."""
    if not os.path.isdir(directory):
        return []
    out = []
    for f in sorted(os.listdir(directory)):
        tail = f".{name}.conf"
        i = f.find(tail)
        if i <= 0:
            continue
        rest = f[i + len(tail):]
        if (rest == "" or (rest.startswith(".") and rest.endswith(".bak"))) and "." not in f[:i]:
            out.append(os.path.join(directory, f))
    return out


def cmd_remove(ctx: Ctx, words: List[str]) -> None:
    """Remove a client: peer (live and wg0.conf), routing rules, keys and config."""
    name = get_name(ctx, words, "remove")
    ops.take_lock(ctx)
    c = ctx.snap.client(name)
    if c is None:
        ctx.out.info(f"no client '{name}'")
        return
    ctx.runner.step(f"Remove client '{name}' ({c.ip})", f"wgt client add {name} (issues new keys)")
    ops.confirm(ctx, f"Remove {name}?")
    # Peer first, then its exit rules: no packet of it may fall back to the default exit.
    if ops.wg_up(ctx):
        ctx.runner.run(["wg", "set", ctx.cfg.WG_IF, "peer", c.pub, "remove"])
        ctx.runner.sh_lines(rules.client_rule_cmds(ctx.cfg, ctx.snap, c, up=False))
    ops.remove_client(ctx, name)
    if ctx.snap.is_disabled(name):
        ops.set_disabled(ctx, name, False)
    ops.set_nowarp(ctx, name, False)
    # After rollback no mode is saved; the next setup rebuilds wg0.conf from clients.db.
    if ops.is_set_up(ctx):
        ops.write_wg_conf(ctx)
    else:
        ctx.out.info(f"server not set up; {ctx.cfg.wg_conf} is rebuilt at the next setup")
    # The config (under any of its names), its preshared key and every copy
    # of them hold this client's secrets.
    files = []
    for d in (ctx.paths.client_dir, ctx.files.copies_dir(ctx.paths.client_dir)):
        files += removal_targets(name, d)
    ctx.files.remove(*files)
    # forget: the file, its change copies and its backup record (nothing for rollback to restore).
    ctx.files.forget(client_conf(ctx, name))
    ctx.files.forget(ctx.paths.psk(name))


def cmd_rename(ctx: Ctx, words: List[str]) -> None:
    """Rename a client, keeping its keys and address (the device's config keeps
    working; only its file name and comments change). A new <code>. prefix
    moves it to that exit."""
    usage = "wgt client rename <name> <new-name>"
    if len(words) < 2:
        raise WgtError(f"usage: {usage}")
    ops.no_more_words(words[2:], usage)
    old, new = words[0], words[1]
    valid_name(old)
    valid_name(new)
    ops.take_lock(ctx)
    ops.require_server(ctx)
    c = ctx.snap.client(old)
    if c is None:
        raise WgtError(f"no client '{old}' (see: wgt client list)")
    if ctx.snap.client(new):
        raise WgtError(f"a client '{new}' already exists")
    code = store.client_exit(new)
    if code and ctx.snap.exit(code) is None:
        raise WgtError(f"no jump '{code}' on this server (see: wgt jump list)")
    conf, newconf = client_conf(ctx, old), client_conf(ctx, new)
    text = store.read_text(conf)
    if text is None:
        raise WgtError(f"no config for '{old}' ({conf})")
    psk = store.read_text(ctx.paths.psk(old)) or ""
    if not psk.strip():
        raise WgtError(f"'{old}' has no preshared key ({ctx.paths.psk(old)}); rotate it instead")
    new_c = store.Client(new, c.ip, c.pub, c.allowed)
    off = ctx.snap.warp_off(old)
    ctx.runner.step(f"Rename client '{old}' to '{new}' (same keys and address; exit: {exit_short(ctx, new, off)})",
                    f"wgt client rename {new} {old}")
    text = re.sub(r"(?m)^# wireguard-tools client: .*$",
                  f"# wireguard-tools client: {new} on {ctx.cfg.SERVER_NAME}", text)
    text = re.sub(r"(?m)^# exit: .*$", f"# exit: {render.exit_desc(ctx.snap, new, off)}", text)
    ctx.files.write(newconf, 0o600, text)
    ctx.files.write(ctx.paths.psk(new), 0o600, psk)
    was_disabled = ctx.snap.is_disabled(old)
    old_down = rules.client_rule_cmds(ctx.cfg, ctx.snap, c, up=False)
    old_table = rules.client_table(ctx.snap, c)
    ops.remove_client(ctx, old)
    ops.set_nowarp(ctx, old, False)
    ops.set_nowarp(ctx, new, off)
    ops.save_client(ctx, new_c)
    if was_disabled:
        ops.set_disabled(ctx, old, False)
        ops.set_disabled(ctx, new, True)
    ops.write_wg_conf(ctx)
    # Moving between exits: the new exit's rules go in before the old ones come
    # out, so the client never falls back to the default exit in between.
    if ops.wg_up(ctx) and old_table != rules.client_table(ctx.snap, new_c):
        ctx.runner.sh_lines(rules.client_rule_cmds(ctx.cfg, ctx.snap, new_c, up=True))
        ctx.runner.sh_lines(old_down)
    ctx.files.forget(conf)
    ctx.files.forget(ctx.paths.psk(old))
    ctx.out.info(f"config now: {newconf} (same keys: the device's tunnel keeps working; rename it there if you like)")


def _set_disabled(ctx: Ctx, words: List[str], disable: bool) -> None:
    """Disable (cut off now) or enable a client. Keys, config and address stay;
    only its peer leaves (or rejoins) wg0, live and in wg0.conf."""
    verb = "disable" if disable else "enable"
    name = get_name(ctx, words, verb)
    ops.take_lock(ctx)
    ops.require_server(ctx)
    c = ctx.snap.client(name)
    if c is None:
        raise WgtError(f"no client '{name}' (see: wgt client list)")
    if ctx.snap.is_disabled(name) == disable:
        ctx.out.info(f"'{name}' is already {verb}d")
        return
    if disable:
        ctx.runner.step(f"Disable client '{name}' ({c.ip}): cut off now; keys, config and address kept",
                        f"wgt client enable {name}")
        ops.confirm(ctx, f"Disable {name}?")
    else:
        ctx.runner.step(f"Enable client '{name}' ({c.ip}) with its existing config", f"wgt client disable {name}")
    ops.set_disabled(ctx, name, disable)
    ops.write_wg_conf(ctx)
    if not ops.wg_up(ctx):
        ctx.out.info(f"{ctx.cfg.WG_IF} is down; applies when it starts")
    elif disable:
        ctx.runner.run(["wg", "set", ctx.cfg.WG_IF, "peer", c.pub, "remove"])
        ctx.out.info(f"peer removed from {ctx.cfg.WG_IF} (other clients unaffected)")
    else:
        ops.add_live_peer(ctx, name, c.pub, c.ip)


def cmd_disable(ctx: Ctx, words: List[str]) -> None:
    """wgt client disable [name]"""
    _set_disabled(ctx, words, True)


def cmd_enable(ctx: Ctx, words: List[str]) -> None:
    """wgt client enable [name]"""
    _set_disabled(ctx, words, False)


def cmd_warp(ctx: Ctx, words: List[str]) -> None:
    """warp <name> on|off: whether a client's traffic goes through a WARP of
    ours (on), or straight out of its exit (off: this server's IP, or its
    jump's tunnel). Live; keys, address and config stay valid."""
    usage = "wgt client warp <name> on|off"
    if len(words) != 2 or words[1] not in ("on", "off"):
        raise WgtError(f"usage: {usage}")
    name, off = words[0], words[1] == "off"
    valid_name(name)
    ops.take_lock(ctx)
    ops.require_server(ctx)
    set_warp(ctx, name, off)


def set_warp(ctx: Ctx, name: str, off: bool) -> None:
    """Switch client <name>'s WARP: the new rules go in before the old come out."""
    c = ctx.snap.client(name)
    if c is None:
        raise WgtError(f"no client '{name}' (see: wgt client list)")
    if ctx.snap.warp_off(name) == off:
        ctx.out.info(f"'{name}' already has WARP {'off' if off else 'on'} (exit: {exit_short(ctx, name)})")
        return
    old_down = rules.client_rule_cmds(ctx.cfg, ctx.snap, c, up=False)
    ctx.runner.step(f"Switch WARP {'off' if off else 'on'} for '{name}' "
                    f"(exit: {exit_short(ctx, name)} -> {exit_short(ctx, name, off)})",
                    f"wgt client warp {name} {'on' if off else 'off'}")
    ops.set_nowarp(ctx, name, off)
    if not off and not c.exit and not ctx.cfg.warp_on:
        ctx.out.info("note: this server's WARP is off (wgt warp on), so it still leaves via this server's IP")
    ops.write_wg_conf(ctx)
    if ops.wg_up(ctx):
        ctx.runner.sh_lines(rules.client_rule_cmds(ctx.cfg, ctx.snap, c, up=True))
        ctx.runner.sh_lines(old_down)
    else:
        ctx.out.info(f"{ctx.cfg.WG_IF} is down; applies when it starts")
    conf = client_conf(ctx, name)
    text = store.read_text(conf)
    if text is not None:
        ctx.files.write(conf, 0o600, re.sub(r"(?m)^# exit: .*$", f"# exit: {render.exit_desc(ctx.snap, name)}", text))


def cmd_show(ctx: Ctx, words: List[str]) -> None:
    """A client's config with keys redacted (--qr adds the QR code), and how to fetch it."""
    show(ctx, get_name(ctx, words, "show"), qr=bool(ctx.args.qr))


def show(ctx: Ctx, name: str, qr: bool) -> None:
    """Print <name>'s config (keys redacted), its QR code if <qr>, and how to fetch it."""
    conf = client_conf(ctx, name)
    text = store.read_text(conf)
    if text is None:
        raise WgtError(f"no config for '{name}' (see: wgt client list)")
    ctx.out.info(conf)
    ctx.out.line()
    sys.stdout.write(system.redact(text))
    if qr:
        ctx.out.line()
        show_qr(ctx, conf)
    ctx.out.line()
    fetch_hint(ctx, conf)


def cmd_share(ctx: Ctx, words: List[str]) -> None:
    """Send a client's config with magic-wormhole (one-time code; 10 minutes)."""
    name = get_name(ctx, words, "share")
    conf = client_conf(ctx, name)
    if not os.path.isfile(conf):
        raise WgtError(f"no config for '{name}' (see: wgt client list)")
    if not shutil.which("wormhole"):
        raise WgtError("magic-wormhole not installed (apt-get install magic-wormhole)")
    if ctx.runner.dry_run:
        ctx.out.info(f"[dry-run] would run: wormhole send {conf}")
        return
    ctx.out.warn(f"this sends '{name}''s private key; give the code only to that person")
    ctx.out.info(f"receiver runs:  wormhole receive <code>   (then imports {os.path.basename(conf)} into WireGuard)")
    ctx.out.info("waiting up to 10 minutes; Ctrl-C cancels")
    # --foreground keeps wormhole in the terminal's process group, so Ctrl-C reaches it.
    rc, _ = ctx.runner.run(["timeout", "--foreground", "600", "wormhole", "send", "--code-length", "3", conf],
                           check=False)
    if rc != 0:
        raise WgtError("not received (cancelled or timed out); nothing was sent")


def cmd_list(ctx: Ctx, words: List[str]) -> None:
    """The clients table (or --tsv rows), then in a terminal the action menu."""
    ops.no_more_words(words, "wgt client list [--tsv]")
    if ctx.args.tsv:
        for row in client_rows(ctx):
            ctx.out.line(row.tsv())
        return
    names = print_clients(ctx, numbered=True)
    if names and sys.stdout.isatty() and not ctx.args.yes and ui.have_tty():
        list_menu(ctx, names)


# The action menu after `client list`: (action, what it does). "toggle" shows
# as enable or disable, depending on the client.
ACTIONS = (
    ("show", "config, keys redacted"),
    ("qr", "config as a QR code"),
    ("share", "send with magic-wormhole"),
    ("rotate", "new keys; the current config stops working"),
    ("toggle", ""),
    ("warp", ""),
    ("rename", "new name, same keys"),
    ("remove", "delete the client, its keys and config"),
)


def list_menu(ctx: Ctx, names: List[str]) -> None:
    """After list: pick a client by number, then an action by number; runs one."""
    try:
        n = ui.pick_number("\n  Client # (Enter to exit): ", len(names))
    except ValueError as e:
        raise WgtError(f"no client number '{e}'") from None
    if n is None:
        return
    name = names[n - 1]
    disabled = ctx.snap.is_disabled(name)
    toggle = (("enable", "let back in with its existing config") if disabled
              else ("disable", "cut off now, keep keys and address"))
    off = ctx.snap.warp_off(name)
    warp = ("warp on", "back through WARP") if off else ("warp off", "straight out of its exit, no WARP")
    items = [f"{label:<9} {what}" for label, what in
             (toggle if action == "toggle" else warp if action == "warp" else (action, what)
              for action, what in ACTIONS)]
    try:
        i = ui.menu(f"{name}:", items, "Action")
    except ValueError as e:
        raise WgtError(f"no action '{e}'") from None
    if i is None:
        return
    ctx.out.line()
    action = ACTIONS[i][0]
    if action in ("show", "qr"):
        show(ctx, name, qr=action == "qr")
    elif action == "share":
        cmd_share(ctx, [name])
    elif action == "rotate":
        cmd_rotate(ctx, [name])
    elif action == "toggle":
        _set_disabled(ctx, [name], not disabled)
    elif action == "warp":
        ops.take_lock(ctx)
        set_warp(ctx, name, not off)
    elif action == "rename":
        new = ui.ask("    New name: ")
        if new:
            cmd_rename(ctx, [name, new])
    elif action == "remove":
        cmd_remove(ctx, [name])


def cmd_client(ctx: Ctx, words: List[str]) -> Optional[int]:
    """wgt client <command>: dispatch; no command means list."""
    return ops.dispatch(ctx, "client", words, CLIENT_COMMANDS)


CLIENT_COMMANDS = {
    "list": cmd_list, "add": cmd_add, "show": cmd_show, "share": cmd_share, "rotate": cmd_rotate,
    "rename": cmd_rename, "disable": cmd_disable, "enable": cmd_enable, "remove": cmd_remove,
    "warp": cmd_warp,
}
