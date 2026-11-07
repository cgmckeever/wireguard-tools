"""What must be true after commands, on a host that changes as they run: the
running state is what booting wg0.conf would give, PostDown leaves nothing, and
no file outlives its config. Then the same with the process killed at every step
of each command, and the command re-run: it has to finish the job.

A new command or hook line gets all of this by being added to the lists here."""

from __future__ import annotations

import contextlib
import copy
import functools
import os
import re
import shutil
import signal

import pytest
from conftest import FIXTURES
from fakehost import Died, Host, Kernel

from wgt import checks, cli, devices, exits, host, ops, render, results, rules, server, store, system, units
from wgt.const import ALL_WATCHDOGS, EXIT_WATCHDOG, Paths
from wgt.errors import WgtError
from wgt.host import Facts

FACTS = Facts(kernel_ipv6=True, has_ipv6=True, hostname="boba", routes4="",
              default_route="default via 192.0.2.1 dev eth0")


class World:
    """A fixture server on a fake host, already running."""

    def __init__(self, tmp, monkeypatch, case="exits"):
        self.tmp = str(tmp)
        wg = os.path.join(self.tmp, "wg")
        shutil.copytree(os.path.join(FIXTURES, case, "wg"), wg)
        self.paths = Paths(wg_dir=wg, backup_dir=f"{self.tmp}/bak", lock_file=f"{self.tmp}/lock",
                           run_dir=f"{self.tmp}/run", hosts_file=f"{self.tmp}/hosts",
                           cloud_cfg_dir=f"{self.tmp}/cloud", systemd_dir=f"{self.tmp}/systemd",
                           sbin_dir=f"{self.tmp}/sbin", sysctl_file=f"{self.tmp}/sysctl",
                           wgcf_bin=f"{self.tmp}/wgcf")
        os.makedirs(self.paths.run_dir)
        with open(self.paths.server_pub, "w") as f:
            f.write("SERVERPUB=\n")
        with open(self.paths.wgcf_bin, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(self.paths.wgcf_bin, 0o755)
        monkeypatch.setattr(host, "resolve4", lambda name: None)
        monkeypatch.setattr("time.sleep", lambda seconds: None)
        self.kernel = Kernel()
        self.host: Host = None
        # Every config has its files, as on a real server.
        snap = store.load(self.paths)
        name = store.state_get(snap.state, "SERVER_NAME")
        os.makedirs(self.paths.client_dir, exist_ok=True)
        os.makedirs(self.paths.psk_dir, exist_ok=True)
        for c in snap.clients:
            with open(self.paths.client_conf(name, c.name), "w") as f:
                f.write(f"# wireguard-tools client: {c.name} on {name}\n# exit: x\n[Interface]\n")
            with open(self.paths.psk(c.name), "w") as f:
                f.write("PSK=\n")
        self.server_name = name
        self.run(["status"], fn=lambda ctx: ops.write_wg_conf(ctx))
        Host(self.kernel, wg).wg0_up(self.read_conf())
        self.run(["device", "set", "mbp-1", "40"])

    def words(self, argv):
        """A command's words, with a jump config to add if it wants one."""
        if "JUMPFILE" not in argv:
            return argv
        path = os.path.join(self.tmp, "jump.conf")
        shutil.copy(self.paths.jump_source("par-fr"), path)
        return [path if w == "JUMPFILE" else w for w in argv]

    def free_the_jump(self, code):
        for c in store.load(self.paths).exit_clients(code):
            self.run(["client", "remove", c.name])

    def read_conf(self) -> str:
        return store.read_text(self.paths.wg("wg0.conf")) or ""

    def run(self, argv, fn=None, die_at=None, handshakes=None, dry_run=False, monkeypatch=None):
        """One process: its own ctx and runner, on the same kernel and files."""
        self.host = Host(self.kernel, self.paths.wg_dir, die_at=die_at, handshakes=handshakes, dry_run=dry_run)
        ctx = cli.load(cli.parse(list(argv) + ["--yes", "--no-qr"]), paths=self.paths, runner=self.host,
                       env={}, facts=FACTS)
        try:
            return fn(ctx) if fn else cli.command(argv[0])(ctx, ctx.args.words[1:])
        finally:
            ops.release_lock(ctx)

    def problems(self):
        """Everything that should hold on a server at rest."""
        found = []
        fresh = Kernel()
        Host(fresh, self.paths.wg_dir).wg0_up(self.read_conf())
        now, boot = self.kernel.view(), fresh.view()
        for key in now:
            if now[key] != boot[key]:
                found.append(f"running {key} differ from booting wg0.conf")
        down = copy.deepcopy(self.kernel)
        Host(down, self.paths.wg_dir).wg0_down(self.read_conf())
        left = down.rules + sorted(down.routes) + [k for k, v in down.chains.items()
                                                   if [r for r in v if not r.startswith(("-s ", "-d "))]]
        if left:
            found.append(f"PostDown leaves {left[:4]}")

        def at_rest(ctx):
            tests = checks.Tests(ctx)
            found.extend(f"drift: {d}" for d in tests.wg0_drift())
            if system.settings_of(self.read_conf()) != system.settings_of(ops.render_wg_conf(ctx)):
                found.append("wg0.conf isn't what this state renders")
            snap = ctx.snap
            names = {c.name for c in snap.clients}
            for c in snap.clients:
                if not os.path.exists(ctx.paths.client_conf(self.server_name, c.name)):
                    found.append(f"{c.name} has no config file")
                if not (store.read_text(ctx.paths.psk(c.name)) or "").strip():
                    found.append(f"{c.name} has no psk")
            for f in os.listdir(ctx.paths.client_dir):
                if f.endswith(".conf") and f[len(self.server_name) + 1:-5] not in names:
                    found.append(f"config file with no entry: {f}")
            for f in os.listdir(ctx.paths.psk_dir):
                if f[:-4] not in names:
                    found.append(f"psk with no entry: {f}")
            found.extend(f"disabled line with no entry: {n}" for n in snap.disabled if n not in names)
            found.extend(f"number with no config: {d.name}" for d in snap.devices if not snap.device_clients(d.name))
            found.extend(f"access: {w}" for w in devices.access_wrong(ctx))
            codes = {e.code for e in snap.exits}
            for f in os.listdir(ctx.paths.exit_dir):
                base = f.split(".")[0]
                if base not in codes and base != "incoming":
                    found.append(f"jump file with no jump: {f}")
        self.run(["status"], fn=at_rest)
        return found


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


# Each: the command, and what must be true after it (given the World's snapshot).
def has(*names):
    return lambda snap: all(snap.client(n) for n in names)


def hasnt(*names):
    return lambda snap: not any(snap.client(n) for n in names)


JOURNEY = [
    (["client", "add", "zed", "--exit", "host,par-fr"], has("zed", "warp.zed", "par-fr.zed", "warp.par-fr.zed")),
    (["client", "add", "tab", "--warp", "off", "--group", "home"], has("tab")),
    (["device", "group", "zed", "home,server"], lambda s: "server" in s.device_groups("zed")),
    (["client", "rotate", "zed"], has("zed")),
    (["client", "disable", "warp.zed"], lambda s: s.is_disabled("warp.zed")),
    (["client", "rotate", "warp.zed"], lambda s: s.is_disabled("warp.zed")),
    (["client", "enable", "warp.zed"], lambda s: not s.is_disabled("warp.zed")),
    (["client", "rename", "par-fr.zed", "warp.par-fr.zed9"], has("warp.par-fr.zed9")),
    (["client", "rename", "tab", "warp.tab"], has("warp.tab")),
    (["client", "rename", "warp.tab", "par-fr.tab"], has("par-fr.tab")),
    (["device", "set", "zed", "77"], lambda s: s.device_number("zed") == 77),
    (["device", "rename", "zed", "zed3"],
     lambda s: s.device_number("zed3") == 77 and "home" in s.device_groups("zed3")),
    (["device", "ungroup", "zed3"], lambda s: not s.device_groups("zed3")),
    (["client", "remove", "zed3"], hasnt("zed3")),
    (["client", "remove", "warp.zed3"], hasnt("warp.zed3")),
    (["client", "remove", "warp.par-fr.zed3"], hasnt("warp.par-fr.zed3")),
    (["client", "remove", "par-fr.tab"], hasnt("par-fr.tab")),
    (["jump", "add", "lon-gb", "JUMPFILE", "--label", "London"], lambda s: s.exit("lon-gb")),
    (["client", "add", "lon-gb.tv3"], has("lon-gb.tv3")),
    (["client", "add", "warp.lon-gb.tv3"], has("warp.lon-gb.tv3")),
    (["client", "remove", "lon-gb.tv3"], hasnt("lon-gb.tv3")),
    (["client", "remove", "warp.lon-gb.tv3"], hasnt("warp.lon-gb.tv3")),
    (["jump", "remove", "lon-gb"], lambda s: not s.exit("lon-gb")),
    (["client", "remove", "warp.par-fr.tv"], hasnt("warp.par-fr.tv")),   # a disabled one
]


def test_the_host_is_consistent_to_start_with(world):
    assert world.problems() == []


def test_consistent_after_every_command_of_a_long_journey(world):
    for argv, holds in JOURNEY:
        world.run(world.words(argv))
        assert holds(store.load(world.paths)), argv
        assert world.problems() == [], argv


def test_a_second_setup_changes_nothing(world):
    world.run(["setup"])
    before = world.kernel.view(chain_contents=True), world.read_conf()
    calls_at = len(world.host.calls)
    world.run(["setup"])
    assert not any("restart wg-quick@wg0" in c or "stop wg-quick@wg0" in c for c in world.host.calls[calls_at:])
    assert (world.kernel.view(chain_contents=True), world.read_conf()) == before
    assert world.problems() == []


def test_setup_then_purge_with_clients_and_a_jump_leaves_nothing(world):
    """The fixture has jumps and clients: everything setup made goes."""
    world.run(["setup"])
    world.run(["rollback", "--purge"])
    assert world.kernel.rules == [] and world.kernel.routes == set()
    assert "wg0" not in world.kernel.links and world.kernel.peers == {}
    assert not [k for k, v in world.kernel.chains.items() if v], world.kernel.chains
    # What's on disk: nothing of ours but what purge says it leaves.
    left = sorted(os.path.relpath(os.path.join(d, f), world.tmp) for d, _, fs in os.walk(world.tmp) for f in fs)
    assert [f for f in left if f not in ("wgcf", "lock") and not f.startswith("run/")] == [], left


def test_every_hook_line_is_something_wgt_test_or_setup_would_miss(world, monkeypatch):
    """Remove each PostUp route or rule in turn from what's running: drift must notice
    (a line needing an interface that's down is allowed to be absent)."""
    def expectations(ctx):
        return rules.hook_expectations(ctx.cfg, ctx.snap, ctx.pub_if)
    wanted = world.run(["status"], fn=expectations)
    assert len(wanted) > 20
    for family, where, text, needs in wanted:
        if needs and needs not in world.kernel.links:
            continue
        saved = copy.deepcopy(world.kernel)
        if where == "rule":
            shown = [(f, s) for f, s in world.kernel.rules if f == family and rules._rule_as_printed(s) == text]
            assert shown, text
            world.kernel.rules = [x for x in world.kernel.rules if x not in shown]
        else:
            gone = {x for x in world.kernel.routes if x[:2] == (family, where) and x[2].startswith(text)}
            assert gone, (family, where, text)
            world.kernel.routes -= gone
        assert world.run(["status"], fn=lambda ctx: checks.Tests(ctx).wg0_drift()), (family, where, text)
        world.kernel = saved


def test_commands_hold_the_lock_when_they_write(world, monkeypatch):
    """A writer without the lock can lose a watchdog's or another command's change."""
    for name in ("_write_clients", "_write_exits", "set_disabled", "set_device", "rename_client", "rename_device",
                 "write_wg_conf"):
        real = getattr(ops, name)

        def checked(ctx, *a, _real=real, _name=name, **k):
            assert ctx.lock_fd is not None, f"{_name} without the lock"
            return _real(ctx, *a, **k)
        monkeypatch.setattr(ops, name, checked)
    for argv, _holds in JOURNEY:
        world.run(world.words(argv))


# --- killed at every step, then run again ---

CRASHES = [
    (["client", "add", "zed"], has("zed", "warp.zed"), None),
    (["client", "add", "tab", "--group", "home,server"], has("tab", "warp.tab"), None),
    (["client", "rotate", "warp.par-fr.mbp-1"], has("warp.par-fr.mbp-1"), None),
    (["client", "remove", "warp.par-fr.mbp-1"], hasnt("warp.par-fr.mbp-1"), None),
    (["client", "disable", "warp.par-fr.mbp-1"], lambda s: s.is_disabled("warp.par-fr.mbp-1"), None),
    (["client", "enable", "warp.par-fr.tv"], lambda s: not s.is_disabled("warp.par-fr.tv"), None),
    (["client", "rename", "warp.par-fr.mbp-1", "par-fr.mbp-1b"], has("par-fr.mbp-1b"), None),
    (["client", "rename", "warp.par-fr.tv", "par-fr.tv2"],
     lambda s: s.client("par-fr.tv2") and s.is_disabled("par-fr.tv2"), None),
    (["device", "rename", "mbp-1", "mbp-9"], lambda s: not s.device_clients("mbp-1") and s.device_clients("mbp-9"),
     ["device", "rename", "mbp-1", "mbp-9", "--force"]),
    (["device", "set", "mbp-1", "99"], lambda s: s.device_number("mbp-1") == 99, None),
    (["device", "group", "mbp-1", "server"], lambda s: "server" in s.device_groups("mbp-1"), None),
    (["setup"], lambda s: store.state_get(s.state, "SETUP_DONE") == "1", None),
    (["jump", "add", "lon-gb", "JUMPFILE", "--label", "London"], lambda s: s.exit("lon-gb"), None),
    (["jump", "remove", "nyc-us"], lambda s: not s.exit("nyc-us"), None),
]


def steps_of(tmp_path, monkeypatch, argv):
    """How many changes a clean run of <argv> makes."""
    w = World(tmp_path / "count", monkeypatch)
    if "nyc-us" in argv:
        w.free_the_jump("nyc-us")
    w.run(w.words(argv))
    return w.host.steps


@pytest.mark.parametrize("argv,holds,again", CRASHES, ids=lambda x: " ".join(x) if isinstance(x, list) else "")
def test_killed_at_any_step_a_rerun_finishes_the_job(tmp_path, monkeypatch, argv, holds, again):
    monkeypatch.setattr(system, "atomic_write", _ticking(system.atomic_write))
    total = steps_of(tmp_path, monkeypatch, argv)
    assert total > 0
    for die_at in range(1, total + 1):
        w = World(tmp_path / str(die_at), monkeypatch)
        if "nyc-us" in argv:
            w.free_the_jump("nyc-us")
        with pytest.raises(Died):
            w.run(w.words(argv), die_at=die_at)
        w.run(w.words(again or argv))
        assert holds(store.load(w.paths)), f"{argv} killed at step {die_at}"
        assert w.problems() == [], f"{argv} killed at step {die_at}"


_current: list = []


def _ticking(real):
    """Writes count as steps too: a process can die between any two."""
    @functools.wraps(real)
    def write(path, content, mode):
        if _current and _current[-1].host is not None:
            _current[-1].host.tick()
        return real(path, content, mode)
    return write


@pytest.fixture(autouse=True)
def _track_world(monkeypatch):
    """So file writes can find the Host that's running."""
    real_init = World.__init__

    def init(self, *a, **k):
        _current.append(self)
        real_init(self, *a, **k)
    monkeypatch.setattr(World, "__init__", init)
    yield
    _current.clear()


# --- dry run ---

def tree(top):
    found = {}
    for d, _, files in os.walk(top):
        for f in files:
            path = os.path.join(d, f)
            if os.path.basename(path) != "lock":
                with open(path, "rb") as fh:
                    found[path] = fh.read()
    return found


@pytest.mark.parametrize("argv", [["setup"], ["rollback"], ["rollback", "--purge"], ["client", "add", "zed"],
                                  ["client", "remove", "warp.par-fr.mbp-1"], ["client", "rotate", "warp.par-fr.mbp-1"],
                                  ["client", "rename", "warp.par-fr.mbp-1", "phone2"],
                                  ["client", "disable", "warp.par-fr.mbp-1"],
                                  ["device", "set", "mbp-1", "99"], ["device", "group", "mbp-1", "server"],
                                  ["device", "rename", "mbp-1", "mbp-9"], ["watchdog", "exits"], ["watchdog", "warp"],
                                  ["watchdog", "devices"]], ids=" ".join)
def test_a_dry_run_changes_nothing(world, argv):
    """Files (the run dir included), and the running state."""
    before, running = tree(world.tmp), world.kernel.view(chain_contents=True)
    world.run([*argv, "--dry-run"])
    assert tree(world.tmp) == before
    assert world.kernel.view(chain_contents=True) == running


# --- restarting ---

def test_setup_restarting_wg0_asks_once_before_it_stops(world, monkeypatch):
    """Whoever answers may be connected through wg0: nothing can be asked once it's down."""
    conf = world.paths.wg("wg0.conf")
    with open(conf) as f:
        text = f.read()
    with open(conf, "w") as f:
        f.write(text.replace("ListenPort", "# changed\nMTU = 1\nListenPort"))
    Host(world.kernel, world.paths.wg_dir).wg0_down(world.read_conf())
    Host(world.kernel, world.paths.wg_dir).wg0_up(world.read_conf())
    asked = []

    def confirm(ctx, question, default_yes=False):
        asked.append((question, "wg0" in world.kernel.links))
    monkeypatch.setattr(ops, "confirm", confirm)
    world.run(["setup"])
    assert "wg0" in world.kernel.links
    assert all(up for q, up in asked if "wg0" in q), asked


def _needing_a_restart(world):
    conf = world.paths.wg("wg0.conf")
    with open(conf) as f:
        text = f.read()
    with open(conf, "w") as f:
        f.write(text.replace("ListenPort", "MTU = 1\nListenPort"))


def test_a_lost_terminal_cant_stop_setup_while_wg0_stops(world, monkeypatch):
    """The stop takes a second on a real server. Found live: a hangup in that second
    killed setup before anything could bring wg0 back."""
    _needing_a_restart(world)
    during, real = [], Host._exec

    def watching(self, args, check, input=None, capture=False):
        if list(args[:2]) == ["systemctl", "stop"] and "wg-quick@wg0" in args:
            during.append(signal.getsignal(signal.SIGHUP))
        return real(self, args, check, input, capture)
    monkeypatch.setattr(Host, "_exec", watching)
    before = signal.getsignal(signal.SIGHUP)
    world.run(["setup"])
    assert during == [signal.SIG_IGN]
    assert signal.getsignal(signal.SIGHUP) == before


def test_setup_brings_wg0_back_if_stopping_it_fails_half_way(world, monkeypatch):
    _needing_a_restart(world)
    real = Host._exec

    def half_stop(self, args, check, input=None, capture=False):
        if list(args[:2]) == ["systemctl", "stop"] and "wg-quick@wg0" in args:
            real(self, args, check, input, capture)
            raise WgtError("systemctl stop failed")
        return real(self, args, check, input, capture)
    monkeypatch.setattr(Host, "_exec", half_stop)
    with pytest.raises(WgtError):
        world.run(["setup"])
    assert "wg0" in world.kernel.links


def test_setup_brings_wg0_back_if_the_firewall_step_fails(world, monkeypatch):
    from wgt import units
    conf = world.paths.wg("wg0.conf")
    with open(conf) as f:
        text = f.read()
    with open(conf, "w") as f:
        f.write(text.replace("ListenPort", "MTU = 1\nListenPort"))

    def broken(ctx, outs):
        raise OSError("ufw broke")
    monkeypatch.setattr(units, "open_firewall", broken)
    with pytest.raises(OSError):
        world.run(["setup"])
    assert "wg0" in world.kernel.links


def test_no_device_can_handshake_before_the_rules_exist(world):
    """The private key is the last PostUp line: until it's set, no peer can connect."""
    ups = [line for line in world.read_conf().splitlines() if line.startswith("PostUp = ")]
    assert "private-key" in ups[-1] and not any("private-key" in line for line in ups[:-1])


def test_device_rule_stays_in_the_chain_skeleton_contract(world):
    """The watchdog deletes `-s` rules it doesn't want, so the chains' own rules can't start that way."""
    def rows(ctx):
        return rules.device_ipt_rules(ctx.cfg)
    for _cmd, _table, chain, rule in world.run(["status"], fn=rows):
        if chain.startswith("WGT"):
            assert not rule.startswith("-s "), rule



# --- what the reviews found, staying found ---

def test_rotate_killed_after_saving_doesnt_leave_the_old_key_connected(tmp_path, monkeypatch):
    """The reason to rotate is that the old key may be out there."""
    monkeypatch.setattr(system, "atomic_write", _ticking(system.atomic_write))
    w = World(tmp_path / "a", monkeypatch)
    old = store.load(w.paths).client("warp.par-fr.mbp-1").pub
    for die_at in range(1, 30):
        w = World(tmp_path / str(die_at), monkeypatch)
        with contextlib.suppress(Died):
            w.run(["client", "rotate", "warp.par-fr.mbp-1"], die_at=die_at)
        w.run(["client", "rotate", "warp.par-fr.mbp-1"])
        assert old not in w.kernel.peers, f"killed at step {die_at}"


def test_setup_refuses_a_new_interface_name_while_the_old_one_runs(world):
    with pytest.raises(WgtError, match="--wg-if"):
        world.run(["setup", "--wg-if", "wg1"])
    assert "wg0" in world.kernel.links


def test_a_failed_setup_leaves_the_databases_on_the_running_subnet(world, monkeypatch):
    from wgt import server
    before = store.read_text(world.paths.client_db)

    def no_warp(ctx):
        raise WgtError("checksum")
    monkeypatch.setattr(server, "provision_warp", no_warp)
    with pytest.raises(WgtError):
        world.run(["setup", "--subnet", "10.77.0.0/24"])
    assert store.read_text(world.paths.client_db) == before
    assert store.state_get(store.load(world.paths).state, "WG_SUBNET") != "10.77.0.0/24"


# --- the life of a server ---

def test_setup_installs_everything_it_says(world):
    world.run(["setup"])

    def units_written(ctx):
        for path, content in units.unit_files(ctx):
            assert store.read_text(path) == content, path
        for e in ctx.snap.exits:
            assert os.path.exists(ctx.paths.px_conf(e.code)) and os.path.exists(ctx.paths.wx_conf(e.code))
    world.run(["status"], fn=units_written)


def test_rollback_cleans_up_even_if_wg0_died_without_its_postdown(world):
    world.run(["setup"])
    world.kernel.links.discard("wg0")     # gone, with its rules still in place
    world.run(["rollback"])
    assert world.kernel.rules == [] and world.kernel.routes == set()
    assert not [k for k, v in world.kernel.chains.items() if v]
    calls = world.host.calls
    assert not ops_is_set_up(world)
    for name in ALL_WATCHDOGS:
        assert any(c.startswith("systemctl disable --now") and f"{name}.timer" in c for c in calls), name
    for e in ("par-fr", "nyc-us"):
        assert any(f"wg-quick@px-{e}" in c and "stop" in c or f"px-{e}" in c and "down" in c for c in calls), e


def ops_is_set_up(world):
    return world.run(["status"], fn=ops.is_set_up)


def test_rollback_restores_forwarding(world):
    world.run(["setup"])
    with open(world.paths.state, "a") as f:
        f.write("ORIG_IP_FORWARD=0\n")
    world.run(["rollback"])
    assert "sysctl -q -w net.ipv4.ip_forward=0" in world.host.calls


def test_ufw_follows_jumps_and_rollback(world, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/usr/sbin/ufw")
    world.kernel.ufw_active = True
    world.run(["setup"])
    assert "51820/udp" in world.kernel.ufw and "route wg0 warp" in world.kernel.ufw
    world.run(world.words(["jump", "add", "lon-gb", "JUMPFILE", "--label", "London"]))
    assert any("px-lon-gb" in rule for rule in world.kernel.ufw)
    world.run(["jump", "remove", "lon-gb"])
    assert not any("lon-gb" in rule for rule in world.kernel.ufw)
    world.run(["rollback"])
    assert world.kernel.ufw == set()
    assert not store.state_get(store.load(world.paths).state, "UFW")


def test_the_jump_watchdog_runs_only_while_there_are_jumps(world):
    world.run(["setup"])
    timer = os.path.join(world.paths.systemd_dir, f"{EXIT_WATCHDOG}.timer")
    assert os.path.exists(timer)
    add_again = world.words(["jump", "add", "lon-gb", "JUMPFILE", "--label", "London"])
    for e in store.load(world.paths).exits:
        world.free_the_jump(e.code)
        world.run(["jump", "remove", e.code])
    assert not os.path.exists(timer)
    world.run(add_again)
    assert os.path.exists(timer)


def test_setup_with_a_new_port_and_subnet_leaves_nothing_of_the_old(world):
    """wg0 stops with its old config first, or its PostDown wouldn't remove the old rules."""
    world.run(["setup"])
    world.run(["setup", "--port", "51999", "--subnet", "10.77.0.0/24"])
    assert world.problems() == []
    assert not any("10.8.0" in str(x) or "10.2.10" in str(x) for x in (world.kernel.rules, world.kernel.routes))
    assert not [r for rows in world.kernel.chains.values() for r in rows if "--dport 51820" in r]


def test_a_stopped_first_setup_doesnt_count_as_set_up(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "atomic_write", _ticking(system.atomic_write))
    done = False
    for die_at in range(1, 400):
        w = World(tmp_path / str(die_at), monkeypatch)
        ops_state = store.read_text(w.paths.state) or ""
        system.atomic_write.__wrapped__(w.paths.state, store.state_unset(ops_state, "WARP"), 0o600)
        try:
            w.run(["setup"], die_at=die_at)
        except Died:
            if ops_is_set_up_quiet(w):   # only with everything in place
                last = os.path.join(w.paths.systemd_dir, f"{ALL_WATCHDOGS[2]}.timer")
                assert os.path.exists(last) and "wg0" in w.kernel.links, f"killed at step {die_at}"
            continue
        done = True
        assert ops_is_set_up_quiet(w)
        break
    assert done


def ops_is_set_up_quiet(world):
    world.host = None
    return world.run(["status"], fn=ops.is_set_up)


def test_a_patched_client_config_is_what_a_fresh_one_would_be(world):
    """setup patches configs line by line, render writes them whole: they must agree."""
    world.run(["client", "add", "zed", "--warp", "off"])
    world.run(["setup", "--warp-mtu", "1400", "--mtu", "1300", "--dns", "9.9.9.9", "--endpoint", "vpn.example.com",
               "--port", "51999"])

    def compare(ctx):
        c = ctx.snap.client("zed")
        path = ctx.paths.client_conf(ctx.cfg.SERVER_NAME, "zed")
        text = store.read_text(path)
        key = re.search(r"PrivateKey = (\S+)", text).group(1)
        fresh = render.render_client_conf(ctx.cfg, ctx.snap, "zed", c.ip, key, "SERVERPUB=",
                                          store.read_text(ctx.paths.psk("zed")).strip(),
                                          ctx.cfg.ENDPOINT, c.allowed or "0.0.0.0/0, ::/0")
        strip = lambda s: [x for x in s.splitlines() if not x.startswith("PublicKey")]   # noqa: E731
        assert [x for x in strip(text) if x not in strip(fresh)] == []
        assert [x for x in strip(fresh) if x not in strip(text)] == []
    world.run(["setup"], fn=compare)


@pytest.mark.parametrize("key,flag,value", [("WG_DNS", "--dns", "9.9.9.9"), ("WG_MTU", "--mtu", "1300"),
                                            ("WG_PORT", "--port", "51999"), ("ENDPOINT", "--endpoint", "vpn.x.com"),
                                            ("SERVER_NAME", "--name", "other"),
                                            ("WG_SUBNET", "--subnet", "10.77.0.0/24")])
def test_a_setting_devices_hold_says_they_need_new_configs(world, key, flag, value):
    def told(ctx):
        return checks.settings_table(ctx).changes_client_configs
    assert world.run(["setup", flag, value, "--warp-mtu", "1400"], fn=told), key


def test_backup_then_purge_then_restore_brings_the_same_server_back(world, monkeypatch):
    """The whole way, through real tar."""
    world.run(["setup"])
    with open(world.paths.state, "a") as f:
        f.write("UFW=1\n")
    before = {n: store.read_text(p) for n, p in (("clients", world.paths.client_db), ("key", world.paths.server_key),
                                                 ("exits", world.paths.exit_db), ("devices", world.paths.device_db))}
    world.run(["backup"])
    archive = next(os.path.join(world.paths.backup_dir, f) for f in os.listdir(world.paths.backup_dir)
                   if f.endswith(".tar.gz"))
    kept = os.path.join(world.tmp, "kept.tar.gz")
    shutil.copy(archive, kept)
    world.run(["rollback", "--purge"])
    assert not os.path.exists(world.paths.client_db)

    class Exec(Exception):
        pass

    def no_exec(path, args):
        assert "setup" in args
        raise Exec
    monkeypatch.setattr(os, "execv", no_exec)
    os.makedirs(world.paths.wg_dir, exist_ok=True)
    with pytest.raises(Exec):
        world.run(["restore", kept])
    state = store.load(world.paths).state
    for gone in ("UFW", "SETUP_DONE", "WARP", "DEFAULT_ROUTE"):
        assert not store.state_get(state, gone), gone
    assert not ops_is_set_up(world)
    world.run(["setup"])
    assert ops_is_set_up(world)
    for name, path in (("clients", world.paths.client_db), ("key", world.paths.server_key),
                       ("exits", world.paths.exit_db), ("devices", world.paths.device_db)):
        assert store.read_text(path) == before[name], name
    assert world.problems() == []


def test_every_firewall_rule_going_missing_is_noticed(world):
    def wanted(ctx):
        return rules.ipt_rules(ctx.cfg, ctx.pub_if, ctx.snap.exits)
    for cmd, table, chain, rule in world.run(["status"], fn=wanted):
        rows = world.kernel.chains[(cmd, table, chain)]
        at = rows.index(rule)
        del rows[at]
        assert world.run(["status"], fn=lambda ctx: checks.Tests(ctx).wg0_drift()), rule
        rows.insert(at, rule)


def test_wgt_test_fails_when_a_firewall_rule_is_missing(world):
    def wanted(ctx):
        return rules.ipt_rules(ctx.cfg, ctx.pub_if, ctx.snap.exits)[0]

    def failures(ctx):
        seen = []
        checks.Tests(ctx, results.Results(out=seen.append)).run("all")
        return [line for line in seen if "FAIL" in line]
    cmd, table, chain, rule = world.run(["status"], fn=wanted)
    without = len(world.run(["status"], fn=failures))
    world.kernel.chains[(cmd, table, chain)].remove(rule)
    assert len(world.run(["status"], fn=failures)) > without


def test_status_and_log_cover_every_watchdog(world, capsys):
    world.run(["setup"])
    assert set(world.run(["status"], fn=checks.watchdogs)) == set(ALL_WATCHDOGS)


# --- found by a review: what a real server's timing or a bad word can do ---

@pytest.mark.parametrize("word", ["..", ".", "", "../..", "/etc", "par-fr/../.."])
@pytest.mark.parametrize("command", [["jump", "remove"], ["exit", "remove"], ["client", "remove"], ["client", "rotate"],
                                     ["client", "disable"], ["client", "enable"], ["device", "rename"],
                                     ["warp", "rotate"]])
def test_a_bad_name_deletes_nothing(world, monkeypatch, command, word):
    """`jump remove ..` emptied /etc/wireguard without asking: the word named a directory."""
    def asked(ctx, question, default_yes=False):
        raise AssertionError(f"reached a question: {question}")
    monkeypatch.setattr(ops, "confirm", asked)
    before = tree(world.paths.wg_dir)
    extra = ["new-name"] if command == ["device", "rename"] else []
    with pytest.raises(WgtError):
        world.run([*command, word, *extra])
    assert tree(world.paths.wg_dir) == before


def test_setup_restores_group_access_when_it_restarts_wg0(world):
    """PostUp makes the chains empty; `wgt test` right after must not need the watchdog."""
    world.run(["device", "group", "mbp-1", "server"])
    world.run(["setup", "--port", "51999"])
    assert world.run(["test"], fn=devices.access_wrong) == []


def _postup_lines(world):
    return [line[len("PostUp = "):].replace("%i", "wg0") for line in world.read_conf().splitlines()
            if line.startswith("PostUp = ")]


def test_a_watchdog_during_postup_doesnt_bury_allow_rules(world):
    """Nothing locks a boot's or a manual restart's PostUp. The refusal goes in late, above
    anything already there, and the allow rules below it would do nothing, for good."""
    world.run(["device", "group", "mbp-1", "server"])
    post = _postup_lines(world)
    kernel = Kernel()
    host = Host(kernel, world.paths.wg_dir)
    kernel.links.add("wg0")
    chains_made = max(i for i, line in enumerate(post) if " -N " in line)
    for line in post[:chains_made + 1]:
        host.shell_line(line)
    world.kernel = kernel
    world.run(["watchdog"], fn=devices.sync)
    assert not [r for r in kernel.chains[("iptables", "filter", "WGT-SERVER")] if r.startswith("-s ")]
    for line in post[chains_made + 1:]:
        host.shell_line(line)
    world.run(["watchdog"], fn=devices.sync)
    rules_now = kernel.chains[("iptables", "filter", "WGT-SERVER")]
    assert rules_now.index(next(r for r in rules_now if r.startswith("-s "))) \
        < rules_now.index(next(r for r in rules_now if "-j REJECT" in r))
    assert world.run(["test"], fn=devices.access_wrong) == []


def test_allow_rules_already_below_the_refusal_are_noticed_and_moved(world):
    world.run(["device", "group", "mbp-1", "server"])
    chain = world.kernel.chains[("iptables", "filter", "WGT-SERVER")]
    allowed = [r for r in chain if r.startswith("-s ")]
    assert allowed
    chain[:] = [r for r in chain if not r.startswith("-s ")] + allowed
    assert any("below its refusal" in w for w in world.run(["test"], fn=devices.access_wrong))
    world.run(["watchdog"], fn=devices.sync)
    assert world.run(["test"], fn=devices.access_wrong) == []
    assert chain.index(allowed[0]) < next(i for i, r in enumerate(chain) if "-j REJECT" in r)


@pytest.mark.parametrize("failing", ["enable", "restart"])
def test_setup_still_finishes_if_warps_unit_wont_start(world, monkeypatch, failing):
    """The README's promise: plain configs work without WARP."""
    real = Host._answer

    def answer(self, args, input):
        if list(args[:2]) == ["systemctl", failing] and "wg-quick@warp" in args:
            return 1, ""
        return real(self, args, input)
    monkeypatch.setattr(Host, "_answer", answer)
    if failing == "enable":
        world.kernel.links.discard("warp")
    world.run(["setup"])
    assert "wg0" in world.kernel.links
    assert store.state_get(store.read_text(world.paths.state), "SETUP_DONE") == "1"


def test_answering_no_about_warp_still_stops_setup(world, monkeypatch):
    world.kernel.links.discard("warp")

    def no(ctx, question, default_yes=False):
        if "warp" in question:
            raise WgtError("aborted")
    monkeypatch.setattr(ops, "confirm", no)
    with pytest.raises(WgtError):
        world.run(["setup"])


def test_setup_keeps_going_if_a_jump_wont_restart(world, monkeypatch):
    real = Host._answer

    def answer(self, args, input):
        if list(args[:2]) == ["systemctl", "restart"] and any("par-fr" in a for a in args):
            return 1, ""
        return real(self, args, input)
    monkeypatch.setattr(Host, "_answer", answer)
    monkeypatch.setattr(exits, "any_up", lambda ctx, e: True)
    world.run(["setup"])
    assert store.state_get(store.read_text(world.paths.state), "SETUP_DONE") == "1"
    assert os.path.exists(world.paths.unit_file(f"{EXIT_WATCHDOG}.timer"))


def test_removing_a_jump_removes_its_rotated_account_copies(world):
    world.free_the_jump("par-fr")
    world.run(["warp", "rotate", "par-fr"])
    world.run(["jump", "remove", "par-fr"])
    left = [os.path.join(d, f) for d, _, names in os.walk(world.paths.backup_dir) for f in names]
    assert not [path for path in left if "par-fr" in path], left


def test_a_restored_subnet_is_checked_against_this_hosts_routes(world):
    """Saved in the archive, so it isn't new to the settings, but it is to the machine."""
    def check(ctx):
        ctx.facts.routes4 = f"{ctx.cfg.WG_SUBNET} dev ens4 proto kernel scope link src 10.0.0.9\n"
        server.refuse_routed_subnet(ctx)
    world.run(["setup"], fn=check)      # wg0 is up: the route is its own business
    Host(world.kernel, world.paths.wg_dir).wg0_down(world.read_conf())
    with pytest.raises(WgtError, match="already routes"):
        world.run(["setup"], fn=check)


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_nothing_interrupts_setup_between_wg0s_stop_and_start(world, monkeypatch, sig):
    conf = world.paths.wg("wg0.conf")
    with open(conf) as f:
        text = f.read()
    with open(conf, "w") as f:
        f.write(text.replace("ListenPort", "MTU = 1\nListenPort"))
    during, real = [], Host._exec

    def watching(self, args, check, input=None, capture=False):
        if list(args[:2]) == ["systemctl", "stop"] and "wg-quick@wg0" in args:
            during.append(signal.getsignal(sig))
        return real(self, args, check, input, capture)
    monkeypatch.setattr(Host, "_exec", watching)
    before = signal.getsignal(sig)
    world.run(["setup"])
    assert during == [signal.SIG_IGN]
    assert signal.getsignal(sig) == before
