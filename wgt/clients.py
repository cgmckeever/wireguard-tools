"""`wgt client ...`: the clients of this server. Each has a keypair, a
preshared key and an address; its config is
/etc/wireguard/clients/<server>.<name>.conf. A client named <code>.<name>
exits through exit city <code>. Changes apply live; other clients stay
connected."""

from __future__ import annotations

import base64
import os
import re
import sys
from typing import List

from . import checks, net, ops, render, rules, store, ui
from .errors import WgtError


# --- helpers ---

def client_conf(ctx, name: str) -> str:
    return os.path.join(ctx.paths.client_dir, f"{ctx.cfg.SERVER_NAME}.{name}.conf")


def valid_name(name: str) -> None:
    why = store.valid_client_name(name)
    if why:
        raise WgtError(why)


def allowed_list(value: str) -> str:
    """--allowed-ips as a comma list without spaces."""
    return re.sub(r"\s", "", value)


def next_free_ip(ctx, planned: List[str]) -> str:
    """First unused host address (the server takes .1). Addresses handed out
    earlier in this run count as used (a dry run writes nothing)."""
    used = {ctx.cfg.server_ip, *(c.ip for c in ctx.snap.clients), *planned}
    for n in range(ctx.cfg.first_host, ctx.cfg.last_host + 1):
        ip = net.int2ip(n)
        if ip not in used:
            planned.append(ip)
            return ip
    raise WgtError(f"no free addresses left in {ctx.cfg.WG_SUBNET}")


def pick_client(ctx, verb: str) -> str:
    """No name given: a numbered list of clients to pick from (needs a terminal)."""
    clients = sorted(ctx.snap.clients, key=lambda c: (checks.device_of(c.name).lower(), "." in c.name, c.exit))
    if not clients:
        raise WgtError("no clients yet (add one: wgt client add <name>)")
    if not ui.have_tty() or ctx.args.yes:
        raise WgtError(f"usage: wgt client {verb} <name>")
    try:
        i = ui.menu("", [f"{checks.device_of(c.name):<20} {checks.exit_label(ctx, c.name):<6} {c.ip}" for c in clients],
                    f"{verb.capitalize()} which client?")
    except ValueError as e:
        raise WgtError(f"no client number '{e}'")
    if i is None:
        raise WgtError("no client chosen")
    return clients[i].name


def pick_exits(ctx, name: str) -> List[str]:
    """Ask which exit(s) a new client gets: codes, "warp" being the default."""
    codes = [e.code for e in ctx.snap.exits]
    items = [f"{'warp':<6} Cloudflare WARP from {ctx.cfg.SERVER_NAME} (default)"]
    items += [f"{e.code:<6} {e.label}" for e in ctx.snap.exits]
    ui.say("\n".join(["", f"    Exit for {name}:"] + [f"      {i + 1}) {it}" for i, it in enumerate(items)]) + "\n")
    reply = ui.ask("    Exit [Enter = 1 (warp); several: 1,2]: ")
    if not reply:
        return ["warp"]
    out = []
    for part in reply.replace(",", " ").split():
        if not part.isdigit():
            out.append(part)
        elif 1 <= int(part) <= len(items):
            out.append("warp" if int(part) == 1 else codes[int(part) - 2])
        else:
            raise WgtError(f"no exit number {part}")
    return out


def fetch_hint(ctx, conf: str) -> None:
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


def show_qr(ctx, conf: str) -> None:
    """The config as a terminal QR code (it holds the private key). In iTerm2 or
    WezTerm it is drawn as a QR_WIDTH-cell-wide image, streamed so the key
    never touches disk; QR_WIDTH=0 forces text."""
    width = os.environ.get("QR_WIDTH", "32")
    if not ctx.runner.ok(["sh", "-c", "command -v qrencode"]):
        raise WgtError("qrencode not installed")
    if not width.isdigit():
        raise WgtError(f"QR_WIDTH must be a number of terminal columns: '{width}'")
    ctx.out.warn("this QR code contains the client's private key")
    data = qr_data(store.read_text(conf) or "")
    term = f"{os.environ.get('LC_TERMINAL', '')} {os.environ.get('TERM_PROGRAM', '')}"
    if int(width) > 0 and sys.stdout.isatty() and not os.environ.get("TMUX") and re.search(r"iTerm2|iTerm\.app|WezTerm", term):
        import subprocess
        png = subprocess.run(["qrencode", "-t", "PNG", "-s", "4", "-m", "2", "-o", "-"], input=data.encode(),
                             stdout=subprocess.PIPE, check=True).stdout
        sys.stdout.write(f"\033]1337;File=inline=1;width={width};preserveAspectRatio=1:"
                         f"{base64.b64encode(png).decode()}\a\n")
        sys.stdout.flush()
    else:
        sys.stdout.write(ctx.runner.query(["qrencode", "-t", "ansiutf8", "-m", "1"], input=data))


def issue_client(ctx, name: str, ip: str, allowed: str, old_pub: str = "") -> None:
    """Generate keys for <name> at <ip>, write its config, update clients.db,
    wg0.conf and the live interface (<old_pub> is replaced). An exit-city
    client's routing rules go in before its peer, so its first packets can't
    leave through the default exit."""
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
                                        ctx.cfg.values["ENDPOINT"], allowed)
    ctx.files.write(os.path.join(ctx.paths.psk_dir, f"{name}.psk"), 0o600, psk + "\n")
    ctx.files.write(conf, 0o600, content)
    ops.put_client(ctx, name, store.Client(name, ip, pub, allowed))
    ops.write_wg_conf(ctx)
    up = ops.wg_up(ctx)
    if up:
        ctx.runner.sh_lines(rules.exit_client_cmds(ctx.cfg, ctx.snap, ctx.snap.client(name), "up"))
    if ctx.snap.is_disabled(name):
        if up and old_pub:
            ctx.runner.run(["wg", "set", ctx.cfg.WG_IF, "peer", old_pub, "remove"])
        ctx.out.info(f"'{name}' is disabled; the new config works once enabled (wgt client enable {name})")
    elif up:
        if old_pub and old_pub != pub:
            ctx.runner.run(["wg", "set", ctx.cfg.WG_IF, "peer", old_pub, "remove"])
        ops.add_live_peer(ctx, name, pub, ip)
    else:
        ctx.out.warn(f"{ctx.cfg.WG_IF} is not up; the peer is in {ctx.cfg.wg_conf} and loads when it starts")
    # QR by default when a person is watching; --qr forces it, --no-qr skips it.
    if ctx.args.qr == 1 or (ctx.args.qr == 0 and sys.stdout.isatty()):
        if ctx.args.qr == 1 or ctx.runner.ok(["sh", "-c", "command -v qrencode"]):
            show_qr(ctx, conf)
        else:
            ctx.out.warn("qrencode not installed; no QR code")
    print("")
    ctx.out.info(f"client config: {conf}")
    fetch_hint(ctx, conf)


# --- commands ---

def cmd_add(ctx, words: List[str]) -> None:
    """add <name> [--exit <code>[,...]]: one client per exit (warp = the
    default), named <name> and <code>.<name>; asks when exit cities exist."""
    if not words:
        raise WgtError("usage: wgt client add <name> [--exit <code>[,...]]")
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
        codes = ["warp"]
    uniq: List[str] = []
    for code in codes:
        if code != "warp" and ctx.snap.exit(code) is None:
            raise WgtError(f"no exit city '{code}' on this server (see: wgt exit list)")
        if code not in uniq:
            uniq.append(code)
    ops.resolve_endpoint(ctx, save=False)
    planned: List[str] = []
    for code in uniq:
        add_one(ctx, base if code == "warp" else f"{code}.{base}", planned)


def add_one(ctx, name: str, planned: List[str]) -> None:
    """Add one client (skipped if it exists)."""
    existing = ctx.snap.client(name)
    if existing:
        ctx.out.info(f"client '{name}' already exists ({existing.ip}); config: {client_conf(ctx, name)}")
        ctx.out.info(f"use 'wgt client rotate {name}' to issue new keys")
        return
    ip = next_free_ip(ctx, planned)
    ctx.runner.step(f"Add client '{name}' at {ip} (exit: {checks.exit_label(ctx, name)})", f"wgt client remove {name}")
    issue_client(ctx, name, ip, allowed_list(ctx.cfg.CLIENT_ALLOWED_IPS))
    tunnel = f"{ctx.cfg.SERVER_NAME}.{name}"
    if len(tunnel) > 15:
        ctx.out.info(f"note: Android's WireGuard app takes tunnel names up to 15 characters; '{tunnel}' is "
                     f"{len(tunnel)}, so shorten it when importing there")


def get_name(ctx, words: List[str], verb: str) -> str:
    name = words[0] if words else pick_client(ctx, verb)
    valid_name(name)
    return name


def cmd_rotate(ctx, words: List[str]) -> None:
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
    ops.resolve_endpoint(ctx, save=False)
    ctx.runner.step(f"Rotate keys for '{name}' ({c.ip}); its current config stops working", "none (old keys are discarded)")
    ops.confirm(ctx, f"Rotate keys for {name}?")
    issue_client(ctx, name, c.ip, allowed, c.pub)


def removal_targets(name: str, directory: str) -> List[str]:
    """Which files in <directory> belong to client <name>: <server>.<name>.conf,
    <name>.conf and their .bak copies. The part before ".<name>.conf" must be a
    server name (no dot), so removing "laptop" leaves "<server>.par-fr.laptop.conf"."""
    if not os.path.isdir(directory):
        return []
    out = []
    for f in sorted(os.listdir(directory)):
        if f == f"{name}.conf" or (f.startswith(f"{name}.conf.") and f.endswith(".bak")):
            out.append(os.path.join(directory, f))
            continue
        tail = f".{name}.conf"
        i = f.find(tail)
        if i <= 0:
            continue
        rest = f[i + len(tail):]
        if (rest == "" or (rest.startswith(".") and rest.endswith(".bak"))) and "." not in f[:i]:
            out.append(os.path.join(directory, f))
    return out


def cmd_remove(ctx, words: List[str]) -> None:
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
        ctx.runner.sh_lines(rules.exit_client_cmds(ctx.cfg, ctx.snap, c, "down"))
    ops.put_client(ctx, name)
    if ctx.snap.is_disabled(name):
        ops.set_disabled(ctx, name, False)
    # After rollback no mode is saved; the next setup rebuilds wg0.conf from clients.db.
    if ctx.cfg.saved.get("WARP"):
        ops.write_wg_conf(ctx)
    else:
        ctx.out.info(f"server not set up; {ctx.cfg.wg_conf} is rebuilt at the next setup")
    # The config, preshared key and their copies all hold this client's secrets.
    files = []
    for d in (ctx.paths.client_dir, ctx.files.change_dir(ctx.paths.client_dir + "/x")):
        files += removal_targets(name, d)
    for d in (ctx.paths.psk_dir, ctx.files.change_dir(ctx.paths.psk_dir + "/x")):
        if os.path.isdir(d):
            files += [os.path.join(d, f) for f in sorted(os.listdir(d))
                      if f == f"{name}.psk" or (f.startswith(f"{name}.psk.") and f.endswith(".bak"))]
    ctx.files.remove(*files)
    # Nothing left for rollback to restore: drop them from the backup record.
    ctx.files.forget(client_conf(ctx, name))
    ctx.files.forget(os.path.join(ctx.paths.psk_dir, f"{name}.psk"))


def cmd_rename(ctx, words: List[str]) -> None:
    """Rename a client, keeping its keys and address (the device's config keeps
    working; only its file name and comments change). A new <code>. prefix
    moves it to that exit."""
    if len(words) < 2:
        raise WgtError("usage: wgt client rename <name> <new-name>")
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
        raise WgtError(f"no exit city '{code}' on this server (see: wgt exit list)")
    conf, newconf = client_conf(ctx, old), client_conf(ctx, new)
    text = store.read_text(conf)
    if text is None:
        raise WgtError(f"no config for '{old}' ({conf})")
    ctx.runner.step(f"Rename client '{old}' to '{new}' (same keys and address; exit: {checks.exit_label(ctx, new)})",
                    f"wgt client rename {new} {old}")
    up = ops.wg_up(ctx)
    if up:
        ctx.runner.sh_lines(rules.exit_client_cmds(ctx.cfg, ctx.snap, c, "down"))
    text = re.sub(r"(?m)^# wireguard-tools client: .*$", f"# wireguard-tools client: {new} on {ctx.cfg.SERVER_NAME}", text)
    text = re.sub(r"(?m)^# exit: .*$", f"# exit: {render.exit_desc(ctx.snap, new)}", text)
    ctx.files.write(newconf, 0o600, text)
    psk = store.read_text(os.path.join(ctx.paths.psk_dir, f"{old}.psk")) or ""
    ctx.files.write(os.path.join(ctx.paths.psk_dir, f"{new}.psk"), 0o600, psk)
    ops.put_client(ctx, old)
    ops.put_client(ctx, new, store.Client(new, c.ip, c.pub, c.allowed))
    if ctx.snap.is_disabled(old):
        ops.set_disabled(ctx, old, False)
        ops.set_disabled(ctx, new, True)
    ops.write_wg_conf(ctx)
    if up:
        ctx.runner.sh_lines(rules.exit_client_cmds(ctx.cfg, ctx.snap, ctx.snap.client(new), "up"))
    ctx.files.forget(conf)
    ctx.files.forget(os.path.join(ctx.paths.psk_dir, f"{old}.psk"))
    ctx.out.info(f"config now: {newconf} (same keys: the device's tunnel keeps working; rename it there if you like)")


def cmd_disable(ctx, words: List[str], disabling: bool) -> None:
    """Disable (cut off now) or enable a client. Keys, config and address stay;
    only its peer leaves (or rejoins) wg0, live and in wg0.conf."""
    verb = "disable" if disabling else "enable"
    name = get_name(ctx, words, verb)
    ops.take_lock(ctx)
    ops.require_server(ctx)
    c = ctx.snap.client(name)
    if c is None:
        raise WgtError(f"no client '{name}' (see: wgt client list)")
    if ctx.snap.is_disabled(name) == disabling:
        ctx.out.info(f"'{name}' is already {verb}d")
        return
    if disabling:
        ctx.runner.step(f"Disable client '{name}' ({c.ip}): cut off now; keys, config and address kept",
                        f"wgt client enable {name}")
        ops.confirm(ctx, f"Disable {name}?")
    else:
        ctx.runner.step(f"Enable client '{name}' ({c.ip}) with its existing config", f"wgt client disable {name}")
    ops.set_disabled(ctx, name, disabling)
    ops.write_wg_conf(ctx)
    if not ops.wg_up(ctx):
        ctx.out.info(f"{ctx.cfg.WG_IF} is down; applies when it starts")
    elif disabling:
        ctx.runner.run(["wg", "set", ctx.cfg.WG_IF, "peer", c.pub, "remove"])
        ctx.out.info(f"peer removed from {ctx.cfg.WG_IF} (other clients unaffected)")
    else:
        ops.add_live_peer(ctx, name, c.pub, c.ip)


def cmd_show(ctx, words: List[str]) -> None:
    """A client's config with keys redacted (--qr adds the QR code), and how to fetch it."""
    from .system import redact
    name = get_name(ctx, words, "show")
    conf = client_conf(ctx, name)
    text = store.read_text(conf)
    if text is None:
        raise WgtError(f"no config for '{name}' (see: wgt client list)")
    ctx.out.info(conf)
    print("")
    sys.stdout.write(redact(text))
    if ctx.args.qr == 1:
        print("")
        show_qr(ctx, conf)
    print("")
    fetch_hint(ctx, conf)


def cmd_share(ctx, words: List[str]) -> None:
    """Send a client's config with magic-wormhole (one-time code; 10 minutes)."""
    name = get_name(ctx, words, "share")
    conf = client_conf(ctx, name)
    if not os.path.isfile(conf):
        raise WgtError(f"no config for '{name}' (see: wgt client list)")
    if not ctx.runner.ok(["sh", "-c", "command -v wormhole"]):
        raise WgtError("magic-wormhole not installed (apt-get install magic-wormhole)")
    if ctx.runner.dry_run:
        ctx.out.info(f"[dry-run] would run: wormhole send {conf}")
        return
    ctx.out.warn(f"this sends '{name}''s private key; give the code only to that person")
    ctx.out.info(f"receiver runs:  wormhole receive <code>   (then imports {os.path.basename(conf)} into WireGuard)")
    ctx.out.info("waiting up to 10 minutes; Ctrl-C cancels")
    # --foreground keeps wormhole in the terminal's process group, so Ctrl-C reaches it.
    rc, _ = ctx.runner.run(["timeout", "--foreground", "600", "wormhole", "send", "--code-length", "3", conf], check=False)
    if rc != 0:
        raise WgtError("not received (cancelled or timed out); nothing was sent")


def cmd_list(ctx, words: List[str]) -> None:
    """The clients table (or --tsv rows), then in a terminal the action menu."""
    if ctx.args.tsv:
        for row in checks.client_rows(ctx):
            print("\t".join(str(x) for x in row))
        return
    names = checks.print_clients(ctx, numbered=True)
    if names and sys.stdout.isatty() and not ctx.args.yes and ui.have_tty():
        list_menu(ctx, names)


ACTIONS = ("show", "qr", "share", "rotate", "toggle", "rename", "remove")


def list_menu(ctx, names: List[str]) -> None:
    """After list: pick a client by number, then an action by number; runs one."""
    try:
        n = ui.pick_number("\n  Client # (Enter to exit): ", len(names))
    except ValueError as e:
        raise WgtError(f"no client number '{e}'")
    if n is None:
        return
    name = names[n - 1]
    disabled = ctx.snap.is_disabled(name)
    items = ["show      config, keys redacted", "qr        config as a QR code", "share     send with magic-wormhole",
             "rotate    new keys; the current config stops working",
             "enable    let back in with its existing config" if disabled else "disable   cut off now, keep keys and address",
             "rename    new name, same keys", "remove    delete the client, its keys and config"]
    try:
        i = ui.menu(f"{name}:", items, "Action")
    except ValueError as e:
        raise WgtError(f"no action '{e}'")
    if i is None:
        return
    print("")
    action = ACTIONS[i]
    if action == "show":
        cmd_show(ctx, [name])
    elif action == "qr":
        ctx.args.qr = 1
        cmd_show(ctx, [name])
    elif action == "share":
        cmd_share(ctx, [name])
    elif action == "rotate":
        cmd_rotate(ctx, [name])
    elif action == "toggle":
        cmd_disable(ctx, [name], not disabled)
    elif action == "rename":
        new = ui.ask("  New name: ")
        if new:
            cmd_rename(ctx, [name, new])
    elif action == "remove":
        cmd_remove(ctx, [name])


def cmd_client(ctx, words: List[str]) -> None:
    sub, rest = (words[0], words[1:]) if words else ("list", [])
    table = {
        "list": cmd_list, "add": cmd_add, "show": cmd_show, "share": cmd_share, "rotate": cmd_rotate,
        "rename": cmd_rename, "remove": cmd_remove,
        "disable": lambda c, w: cmd_disable(c, w, True), "enable": lambda c, w: cmd_disable(c, w, False),
    }
    fn = table.get(sub)
    if fn is None:
        raise WgtError(f"unknown client command: {sub} (list, add, show, share, rotate, rename, disable, enable, remove)")
    fn(ctx, rest)
