"""Static device addresses: each device (all its configs) has one number, and
10.9.8.<number> reaches it whichever config it is using.

A config's own address stays what routing goes by (rules.py), so a static address
is only a forward: the device watchdog points it at the config with the newest
recent handshake, and shows a sender as its own static address, which is what a
warped carrier's AllowedIPs allows ("# device-network:" in client configs).
The numbers live in devices.db; wgt-ctl keeps them the same across servers
(fleet.py). `wgt client add` alone can't see the other servers, so it counts down
from the top of the range and wgt-ctl counts up from the bottom."""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Mapping, Optional, Tuple

from . import live, ops, store
from .const import (
    DEVICE_ACTIVE,
    DEVICE_CHAIN,
    DEVICE_CTL_FIRST,
    DEVICE_DIRECT_LAST,
    DEVICE_SOURCE_CHAIN,
    device_ip,
)
from .errors import WgtError

if TYPE_CHECKING:
    from .ctx import Ctx


def static_of(ctx: Ctx, config: str) -> str:
    """The static address of a config's device ("" if it has none)."""
    number = ctx.snap.device_number(store.device_of(config))
    return "" if number is None else device_ip(number)


def _number_word(word: str) -> int:
    if not word.isdigit() or not store.valid_device_number(int(word)):
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


def drop_if_unused(ctx: Ctx, device: str) -> None:
    """The device's last config is gone: so is its number."""
    if ctx.snap.device_number(device) is not None and not ctx.snap.device_clients(device):
        ops.set_device(ctx, device, None)


def config_renamed(ctx: Ctx, old: str, new: str) -> None:
    """One config moved from device <old> to device <new>. The number follows
    if that was <old>'s last config and <new> has none."""
    number = ctx.snap.device_number(old)
    if ctx.snap.device_clients(old):
        if ctx.snap.device_number(new) is None:
            ensure_number(ctx, new)
        return
    if number is not None:
        ops.set_device(ctx, old, None)
    if ctx.snap.device_number(new) is None:
        if number is not None:
            ops.set_device(ctx, new, number)
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
        configs = [c for c in snap.device_clients(d.name) if not snap.is_disabled(c.name)]
        sources += [f"-s {c.ip}/32 -j SNAT --to-source {static}" for c in configs]
        recent = [c for c in configs if handshakes.get(c.pub, 0) > 0 and now - handshakes[c.pub] <= DEVICE_ACTIVE]
        if recent:
            newest = max(recent, key=lambda c: handshakes[c.pub])
            forwards[static] = f"-d {static}/32 -j DNAT --to-destination {newest.ip}"
    return forwards, sources


def sync(ctx: Ctx) -> List[str]:
    """Make the chains match desired(), new rules before old ones go, so a
    forward never disappears in between. Returns what changed, for the log.
    Nothing to do until wg0 has been restarted with the chains (setup)."""
    r = ctx.runner
    forwards, sources = desired(ctx.snap, live.handshakes(r, ctx.cfg.WG_IF), ctx.clock())
    log: List[str] = []
    for chain, want in ((DEVICE_CHAIN, list(forwards.values())), (DEVICE_SOURCE_CHAIN, sources)):
        have = live.ipt_chain(r, "nat", chain)
        if have is None:
            continue
        for spec in want:
            if spec not in have and r.ok(["iptables", "-t", "nat", "-A", chain, *spec.split()]) \
                    and chain == DEVICE_CHAIN:
                log.append(spec.replace(" -j DNAT --to-destination ", " -> ").replace("-d ", "").replace("/32", ""))
        for spec in have:
            if spec not in want and r.ok(["iptables", "-t", "nat", "-D", chain, *spec.split()]) \
                    and chain == DEVICE_CHAIN and spec.split()[1].split("/")[0] not in forwards:
                log.append(f"{spec.split()[1].split('/')[0]}: no config in use")
    return log


# --- commands ---

def cmd_list(ctx: Ctx, words: List[str]) -> None:
    """Each device and its static address (--tsv: device, number; wgt-ctl reads it)."""
    ops.no_more_words(words, "wgt device list [--tsv]")
    for device in ctx.snap.device_names():
        number = ctx.snap.device_number(device)
        if ctx.args.tsv:
            ctx.out.line(f"{device}\t{'' if number is None else number}")
        else:
            ctx.out.line(f"{device:<32} {'(none)' if number is None else device_ip(number)}")


def cmd_set(ctx: Ctx, words: List[str]) -> None:
    """set <device> [number]: its static number (none given: the next from the
    top of the range). Takes effect within 10s; the device needs no new config."""
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
