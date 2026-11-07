"""Static device addresses: each device (all its configs) has one number, and
10.9.8.<number> reaches it whichever config it is using.

A config's own address stays what routing goes by (rules.py), so a static address
is only a forward: the device watchdog points it at the config with the newest
recent handshake, and shows a sender as its own static address, which is what a
warped carrier's AllowedIPs allows ("# device-network:" in client configs).

Groups say who a device can reach. Two devices can talk if they share a group;
the group "server" reaches the server instead of the others in it; a device with
no groups reaches neither. The watchdog keeps the filter chains matching them.
Groups are per server, unlike numbers: `device list --tsv` gives them as a third
column, which is what wgt-ctl would read to keep them the same across servers.

The numbers live in devices.db; wgt-ctl keeps them the same across servers
(ctl/fleet.py). `wgt client add` alone can't see the other servers, so it counts down
from the top of the range and wgt-ctl counts up from the bottom."""

from __future__ import annotations

from itertools import combinations
from typing import TYPE_CHECKING, Dict, List, Mapping, Optional, Tuple

from . import live, ops, store
from .const import (
    DEVICE_ACTIVE,
    DEVICE_CHAIN,
    DEVICE_CTL_FIRST,
    DEVICE_DIRECT_LAST,
    DEVICE_PEER_CHAIN,
    DEVICE_SERVER_CHAIN,
    DEVICE_SOURCE_CHAIN,
    SERVER_GROUP,
    device_ip,
)
from .errors import WgtError

FAILED = "could not"   # starts sync's lines about a rule it couldn't change

if TYPE_CHECKING:
    from .ctx import Ctx


def static_of(ctx: Ctx, config: str) -> str:
    """The static address of a config's device ("" if it has none)."""
    number = ctx.snap.device_number(store.device_of(config))
    return "" if number is None else device_ip(number)


def _number_word(word: str) -> int:
    if not store.is_uint(word) or not store.valid_device_number(int(word)):
        raise WgtError(f"a device number is {DEVICE_CTL_FIRST}-{DEVICE_DIRECT_LAST}: '{word}'")
    return int(word)


def _owner(ctx: Ctx, number: int) -> str:
    """The device holding <number> on this server ("" if none)."""
    return next((d.name for d in ctx.snap.devices if d.number == number), "")


def _next_direct(ctx: Ctx) -> Optional[int]:
    number = store.next_direct_number(ctx.snap.devices)
    if number is None:
        ctx.out.warn("no static numbers left for devices added here")
    return number


def ensure_number(ctx: Ctx, device: str) -> None:
    """`client add`: give a new device its number: --device-number (from
    wgt-ctl, which knows the fleet's) if free here, otherwise the next from the
    top of the range. An existing device keeps its own."""
    asked = _number_word(ctx.args.device_number) if ctx.args.device_number else None
    have = ctx.snap.device_number(device)
    if have is not None:
        if asked is not None and asked != have:
            ctx.out.warn(f"'{device}' is {device_ip(have)} here, not {device_ip(asked)} as on your other servers "
                         f"(fix: wgt-ctl devices fix {device} <number>)")
        return
    number = asked
    if asked is not None and _owner(ctx, asked):
        ctx.out.warn(f"{device_ip(asked)} is '{_owner(ctx, asked)}' on this server; giving '{device}' another number "
                     f"(fix: wgt-ctl devices fix {device} <number>)")
        number = None
    if number is None:
        number = _next_direct(ctx)
        if number is None:
            return
    ops.set_device(ctx, device, number)
    ctx.out.info(f"device '{device}': always at {device_ip(number)}, whichever config it's using")
    if asked is None:
        ctx.out.info("(number picked on this server, not checked against any others: see wgt-ctl devices)")


def ensure_groups(ctx: Ctx, device: str) -> None:
    """`client add --group`: add the device to these groups."""
    if not ctx.args.groups or ctx.snap.device_number(device) is None:
        return
    groups = store.parse_groups(ctx.args.groups)
    have = ctx.snap.device_groups(device)
    if not set(groups) <= set(have):
        ops.set_device(ctx, device, ctx.snap.device_number(device), tuple(dict.fromkeys(have + groups)))
    ctx.out.info(f"device '{device}': groups {','.join(ctx.snap.device_groups(device))}")


def drop_if_unused(ctx: Ctx, device: str) -> None:
    """The device's last config is gone: so is its number."""
    if ctx.snap.device_number(device) is not None and not ctx.snap.device_clients(device):
        ops.set_device(ctx, device, None)


def config_renamed(ctx: Ctx, old: str, new: str) -> None:
    """One config moved from device <old> to device <new>. The number follows
    if that was <old>'s last config and <new> has none."""
    number = ctx.snap.device_number(old)
    groups = ctx.snap.device_groups(old)
    if ctx.snap.device_clients(old):
        if ctx.snap.device_number(new) is None:
            ensure_number(ctx, new)
        return
    if number is not None:
        ops.set_device(ctx, old, None)
    if ctx.snap.device_number(new) is None:
        if number is not None:
            ops.set_device(ctx, new, number, groups)
        else:
            ensure_number(ctx, new)


# --- the watchdog's rules ---

def desired(snap: store.Snapshot, handshakes: Mapping[str, int], now: float) -> Tuple[Dict[str, str], List[str]]:
    """What the device chains should hold: {static address: DNAT rule} for
    each device with a config in use (the newest handshake, within
    DEVICE_ACTIVE), and a sender's SNAT rule for each enabled config."""
    forwards: Dict[str, str] = {}
    sources: List[str] = []
    for d in snap.devices:
        static = device_ip(d.number)
        configs = _enabled(snap, d.name)
        sources += [f"-s {c.ip}/32 -j SNAT --to-source {static}" for c in configs]
        recent = [c for c in configs if live.seen_within(handshakes, c.pub, DEVICE_ACTIVE, now)]
        if recent:
            newest = max(recent, key=lambda c: handshakes[c.pub])
            forwards[static] = f"-d {static}/32 -j DNAT --to-destination {newest.ip}"
    return forwards, sources


def peer_rules(snap: store.Snapshot) -> List[str]:
    """Traffic through static addresses, both ways, between each pair of devices
    sharing a group other than "server". ctstate DNAT: never by config address."""
    found: List[str] = []
    for a, b in combinations(snap.devices, 2):
        if not (set(a.groups) & set(b.groups)) - {SERVER_GROUP}:
            continue
        for x in _enabled(snap, a.name):
            for y in _enabled(snap, b.name):
                found += [f"-s {x.ip}/32 -d {y.ip}/32 -m conntrack --ctstate DNAT -j ACCEPT",
                          f"-s {y.ip}/32 -d {x.ip}/32 -m conntrack --ctstate DNAT -j ACCEPT"]
    return found


def server_sources(snap: store.Snapshot) -> List[store.Client]:
    """The configs of devices in the "server" group."""
    return [c for d in snap.devices if SERVER_GROUP in d.groups for c in _enabled(snap, d.name)]


def _enabled(snap: store.Snapshot, device: str) -> List[store.Client]:
    return [c for c in snap.device_clients(device) if not snap.is_disabled(c.name)]


def sync(ctx: Ctx) -> List[str]:
    """Make the chains match desired(), new rules before old ones go, so a
    forward never disappears in between. Returns what changed and what failed,
    for the log. Nothing to do until wg0 has been restarted with the chains
    (setup). Takes the caller's lock."""
    r = ctx.runner
    forwards, sources = desired(ctx.snap, live.handshakes(r, ctx.cfg.WG_IF), ctx.clock())
    log: List[str] = []
    for chain, want in ((DEVICE_CHAIN, list(forwards.values())), (DEVICE_SOURCE_CHAIN, sources)):
        have = live.ipt_chain(r, "nat", chain)
        if have is None:
            continue
        for spec in (s for s in want if s not in have):
            if not r.attempt(["iptables", "-t", "nat", "-A", chain, *spec.split()]):
                log.append(f"{FAILED} add to {chain}: {spec}")
            elif chain == DEVICE_CHAIN:
                log.append(spec.replace(" -j DNAT --to-destination ", " -> ").replace("-d ", "").replace("/32", ""))
        for spec in (s for s in have if s not in want):
            if not r.attempt(["iptables", "-t", "nat", "-D", chain, *spec.split()]):
                log.append(f"{FAILED} remove from {chain}: {spec}")
            elif chain == DEVICE_CHAIN and spec.split()[1].split("/")[0] not in forwards:
                log.append(f"{spec.split()[1].split('/')[0]}: no config in use")
    return log + _sync_allowed(ctx)


def apply_now(ctx: Ctx) -> None:
    """After a change here: the firewall follows at once rather than at the
    watchdog's next run, which stays as the retry. With wg0 down there's nothing
    to change: its chains come back with it."""
    if not ops.wg_up(ctx):
        ctx.out.info(f"{ctx.cfg.WG_IF} is down: this applies when it's back up")
        return
    if live.ipt_chain(ctx.runner, "filter", DEVICE_PEER_CHAIN) is None:
        ctx.out.warn(f"{ctx.cfg.WG_IF} is running without the device rules: this applies after: wgt setup")
        return
    failed = [msg for msg in sync(ctx) if msg.startswith(FAILED)]
    for msg in failed:
        ctx.out.warn(f"{msg} (the watchdog keeps retrying; see: wgt log)")
    if not failed and not ctx.runner.dry_run:
        ctx.out.info("applied")


def _allowed_chains(ctx: Ctx) -> List[Tuple[str, str, List[str]]]:
    """What the groups allow, per filter chain: (command, chain, its allow rules)."""
    snap = ctx.snap
    servers = server_sources(snap)
    wanted = [("iptables", DEVICE_PEER_CHAIN, peer_rules(snap)),
              ("iptables", DEVICE_SERVER_CHAIN, [f"-s {c.ip}/32 -j ACCEPT" for c in servers])]
    if ctx.cfg.ipv6:
        wanted.append(("ip6tables", DEVICE_SERVER_CHAIN, [f"-s {ctx.cfg.ip6_of(c.ip)}/128 -j ACCEPT" for c in servers]))
    return wanted


def _refusal_at(have: List[str]) -> Optional[int]:
    """Where a chain's refusal is: allow rules only work above it. None while
    wg0's PostUp is still building the chain."""
    return next((i for i, spec in enumerate(have) if "-j REJECT" in spec), None)


def access_wrong(ctx: Ctx) -> List[str]:
    """Allow rules running that the groups don't want, ones they want that aren't,
    and ones below the refusal, which do nothing. Nothing if the chains aren't
    there (wgt test checks those)."""
    wrong: List[str] = []
    for cmd, chain, want in _allowed_chains(ctx):
        have = live.ipt_chain(ctx.runner, "filter", chain, cmd)
        if have is not None:
            refusal = _refusal_at(have)
            if refusal is not None:
                wrong += [f"{chain} has {s} below its refusal" for s in have[refusal:] if s.startswith("-s ")]
            have = [spec for spec in have if spec.startswith("-s ")]
            wrong += [f"{chain} lacks {s}" for s in want if s not in have]
            wrong += [f"{chain} still has {s}" for s in have if s not in want]
    return wrong


def _sync_allowed(ctx: Ctx) -> List[str]:
    """Make the filter chains match the groups. What's allowed goes in above the
    chain's own rules (the refusal at the end, which this leaves alone). Returns
    what failed: an allow rule that won't go away is a device keeping access.
    A chain PostUp hasn't finished waits for the next pass: its refusal would go
    in above anything added now. One found below the refusal moves above it."""
    failed: List[str] = []
    r = ctx.runner
    for cmd, chain, want in _allowed_chains(ctx):
        have = live.ipt_chain(r, "filter", chain, cmd)
        refusal = None if have is None else _refusal_at(have)
        if have is None or refusal is None:
            continue
        for spec in have[refusal:]:
            if spec.startswith("-s ") and not r.attempt([cmd, "-t", "filter", "-D", chain, *spec.split()]):
                failed.append(f"{FAILED} remove from {chain}: {spec}")
        have = have[:refusal]
        for spec in want:
            if spec not in have and not r.attempt([cmd, "-t", "filter", "-I", chain, *spec.split()]):
                failed.append(f"{FAILED} add to {chain}: {spec}")
        for spec in have:
            if spec.startswith("-s ") and spec not in want \
                    and not r.attempt([cmd, "-t", "filter", "-D", chain, *spec.split()]):
                failed.append(f"{FAILED} remove from {chain}: {spec}")
    return failed


# --- commands ---

def cmd_list(ctx: Ctx, words: List[str]) -> None:
    """Each device and its static address (--tsv: device, number; wgt-ctl reads it)."""
    ops.no_more_words(words, "wgt device list [--tsv]")
    for device in ctx.snap.device_names():
        number = ctx.snap.device_number(device)
        groups = ",".join(ctx.snap.device_groups(device))
        if ctx.args.tsv:
            ctx.out.line(f"{device}\t{'' if number is None else number}" + (f"\t{groups}" if groups else ""))
        else:
            ctx.out.line(f"{device:<32} {'(none)' if number is None else device_ip(number):<14} "
                         f"{groups or 'isolated'}")


def cmd_set(ctx: Ctx, words: List[str]) -> None:
    """set <device> [number]: its static number (none given: the next from the
    top of the range). Takes effect at once; the device needs no new config."""
    usage = "wgt device set <device> [number]"
    if not words or len(words) > 2:
        raise WgtError(f"usage: {usage}")
    device = words[0]
    number = _number_word(words[1]) if len(words) == 2 else None
    ops.take_lock(ctx)
    ops.require_setup(ctx)
    if not ctx.snap.device_clients(device):
        raise WgtError(f"no device '{device}' (see: wgt client list)")
    have = ctx.snap.device_number(device)
    if number is None:
        if have is not None:
            ctx.out.info(f"'{device}' is already {device_ip(have)}")
            return
        number = _next_direct(ctx)
        if number is None:
            raise WgtError("no number to give it")
    owner = _owner(ctx, number)
    if owner and owner != device:
        raise WgtError(f"{device_ip(number)} is already '{owner}'")
    if have != number:
        ctx.runner.step(f"Device '{device}': static address {device_ip(number)}",
                        f"wgt device set {device} {have if have is not None else '<number>'}")
        ops.set_device(ctx, device, number)
        apply_now(ctx)


def cmd_group(ctx: Ctx, words: List[str]) -> None:
    """group [<device> <group,group>]: add a device to groups (none given: list
    every device and its groups). Takes effect at once; no new config."""
    if not words:
        cmd_list(ctx, [])
        return
    if len(words) != 2:
        raise WgtError("usage: wgt device group [<device> <group,group>]")
    device, wanted = words[0], store.parse_groups(words[1])
    number = _for_groups(ctx, device)
    have = ctx.snap.device_groups(device)
    ctx.runner.step(f"Device '{device}': add to {','.join(wanted)}", f"wgt device ungroup {device} {words[1]}")
    ops.set_device(ctx, device, number, tuple(dict.fromkeys(have + wanted)))
    apply_now(ctx)


def cmd_ungroup(ctx: Ctx, words: List[str]) -> None:
    """ungroup <device> [group,group]: remove a device from these groups (none
    given: all of them, which isolates it). Takes effect at once."""
    if len(words) not in (1, 2):
        raise WgtError("usage: wgt device ungroup <device> [group,group]")
    device = words[0]
    number = _for_groups(ctx, device)
    have = ctx.snap.device_groups(device)
    leaving = store.parse_groups(words[1]) if len(words) == 2 else have
    ctx.runner.step(f"Device '{device}': remove from {','.join(leaving) or 'no groups'}",
                    f"wgt device group {device} {','.join(have)}" if have else "nothing to undo")
    ops.set_device(ctx, device, number, tuple(g for g in have if g not in leaving))
    apply_now(ctx)


def _for_groups(ctx: Ctx, device: str) -> int:
    """The number of a device about to change groups, under the lock."""
    ops.take_lock(ctx)
    ops.require_setup(ctx)
    if not ctx.snap.device_clients(device):
        raise WgtError(f"no device '{device}' (see: wgt client list)")
    number = ctx.snap.device_number(device)
    if number is None:
        raise WgtError(f"'{device}' has no static address yet (wgt device set {device})")
    return number
