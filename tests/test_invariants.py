"""What must be true after commands, on a host that changes as they run: the
running state is what booting wg0.conf would give, PostDown leaves nothing, and
no file outlives its config. Then the same with the process killed at every step
of each command, and the command re-run: it has to finish the job.

A new command or hook line gets all of this by being added to the lists here."""

from __future__ import annotations

import contextlib
import copy
import os
import shutil

import pytest
from conftest import FIXTURES
from fakehost import Died, Host, Kernel

from wgt import checks, cli, devices, host, ops, rules, store, system
from wgt.const import Paths
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
