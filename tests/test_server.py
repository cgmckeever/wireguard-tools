"""server.py commands that can be checked without a real server."""

from __future__ import annotations

import io
import os
import shutil
import tarfile
from pathlib import Path

import pytest
from conftest import FIXTURES, KEYS, temp_paths

from wgt import backup, cli, exits, host, ops, render, store, ui, warp_rotate
from wgt import server as srv
from wgt.errors import CommandFailed, WgtError
from wgt.host import Facts
from wgt.system import FakeRunner, Out


def _tar(path, members):
    with tarfile.open(path, "w:gz") as t:
        for name, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "sym":
                info.type, info.linkname = tarfile.SYMTYPE, "/etc/shadow"
                t.addfile(info)
            else:
                data = b"x\n"
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))


def test_restore_refuses_unsafe_archives(make_ctx, tmp_path):
    ctx = make_ctx()
    ops.state_unset(ctx, "WARP")   # not set up
    state = ctx.paths.state.lstrip("/")
    bad = [[(state, "file"), (ctx.paths.wg("..", "..", "root", "x").lstrip("/"), "file")],
           [(state, "file"), (ctx.paths.wg("link").lstrip("/"), "sym")]]
    for i, members in enumerate(bad):
        a = str(tmp_path / f"a{i}.tgz")
        _tar(a, members)
        with pytest.raises(WgtError, match="unsafe entry"):
            backup.cmd_restore(ctx, [a])


def test_restore_needs_a_backup(make_ctx, tmp_path):
    ctx = make_ctx()
    a = str(tmp_path / "x.tgz")
    _tar(a, [("etc/other", "file")])
    with pytest.raises(WgtError, match="not a wireguard-tools backup"):
        backup.cmd_restore(ctx, [a])


def test_comment_only_change_needs_no_restart(make_ctx):
    ctx = make_ctx()
    from wgt import ops
    ops.write_wg_conf(ctx)
    text = Path(ctx.cfg.wg_conf).read_text()
    Path(ctx.cfg.wg_conf).write_text(text.replace("(use wgt client)", "(an edited comment)"))
    assert srv.restart_needed(ctx) is False            # wg0 is up, but only a comment differs
    assert ops.write_wg_conf(ctx) is False                  # rewritten, settings unchanged
    assert "(use wgt client)" in Path(ctx.cfg.wg_conf).read_text()
    assert not any("systemctl stop" in c for c in ctx.runner.changes())


def test_dry_run_remove_shows_wg0_diff(make_ctx, capsys):
    from wgt import clients, ops
    ctx = make_ctx()
    ops.write_wg_conf(ctx)
    capsys.readouterr()
    before = {p: Path(p).read_text() for p in (ctx.cfg.wg_conf, ctx.paths.client_db)}
    ctx.runner.dry_run = True
    clients.cmd_remove(ctx, ["mbp-1"])
    printed = capsys.readouterr().out
    assert "-# mbp-1" in printed                             # wg0.conf diff shows the peer going
    assert all(Path(p).read_text() == t for p, t in before.items())


def test_force_keeps_server_keys(make_ctx):
    from wgt import cli
    ctx = make_ctx(argv=["--force"])
    for p in (ctx.paths.server_key, ctx.paths.server_pub):
        Path(p).write_text("KEY\n")
    assert srv.server_keys_due(ctx) is False
    assert cli.parse(["setup", "--new-server-keys"]).new_server_keys


def test_a_missing_server_pub_comes_from_the_key_wg0_runs(make_ctx):
    """Not a new keypair: that would stop every config, unasked with --yes."""
    ctx = make_ctx(answers={"wg pubkey": (0, "DERIVEDPUB=\n")})
    Path(ctx.paths.server_key).write_text("SERVERKEY=\n")
    os.remove(ctx.paths.server_pub)
    assert srv.server_keys_due(ctx) is False
    assert Path(ctx.paths.server_key).read_text() == "SERVERKEY=\n"
    assert Path(ctx.paths.server_pub).read_text() == "DERIVEDPUB=\n"
    assert "wg genkey" not in ctx.runner.calls


class WgcfRunner(FakeRunner):
    """FakeRunner that plays wgcf: `register` writes an account, `generate` a
    profile (the fixture's), as the real tool does in its working directory."""

    def run_logged(self, args, log, cwd, append=False):
        ok = super().run_logged(args, log, cwd, append)
        if args[1:2] == ["register"]:
            Path(cwd, "wgcf-account.toml").write_text("account\n")
        elif args[1:2] == ["generate"]:
            shutil.copy(os.path.join(FIXTURES, "exits", "wg", "wgcf", "wgcf-profile.conf"), cwd)
        return ok


def tree(root):
    """Every file and directory under <root>, relative."""
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*"))


def test_rollback_purge_undoes_a_fresh_setup(tmp_path, monkeypatch):
    """Setup on an empty server, then rollback --purge: every file setup made
    (configs, keys, state, drop-in, timers, sysctl, backups) is gone again."""
    monkeypatch.setattr(host, "resolve4", lambda name: None)   # no DNS in tests
    monkeypatch.setattr("time.sleep", lambda seconds: None)    # no retry waits
    root = tmp_path / "box"
    for d in ("wg", "systemd", "sbin", "run", "bin"):
        (root / d).mkdir(parents=True)
    wgcf = root / "bin" / "wgcf"
    wgcf.write_text("#!/bin/sh\n")
    wgcf.chmod(0o755)
    paths = temp_paths(root, lock_file=tmp_path / "lock", wgcf_bin=wgcf, sysctl_file=root / "99-wg.conf",
                       backup_dir=root / "backups")
    route = "default via 192.0.2.1 dev eth0 proto static"
    facts = Facts(kernel_ipv6=True, has_ipv6=False, hostname="test", routes4="", default_route=route)
    answers = dict(KEYS, **{"ip -4 route show default": (0, route + "\n")})
    before = tree(root)

    def run(*argv):
        runner = WgcfRunner(answers, out=Out())
        ctx = cli.load(cli.parse([*argv, "--yes"]), paths=paths, runner=runner, env={}, facts=facts)
        try:
            cli.command(argv[0])(ctx, ctx.args.words[1:])
        finally:
            ops.release_lock(ctx)   # a real run releases it by exiting
        return ctx

    ctx = run("setup", "--name", "test", "--endpoint", "203.0.113.1")
    made = set(tree(root)) - set(before)
    assert {"wg/wg0.conf", "wg/warp.conf", "wg/server.key", "wg/wg-tools.state", "99-wg.conf",
            "systemd/wg-tools-warp-watchdog.timer"} <= made, made
    assert ctx.cfg.values["ENDPOINT"] == "203.0.113.1"
    assert ops.is_set_up(ctx) and "SETUP_DONE=1" in (root / "wg" / "wg-tools.state").read_text()
    run("rollback", "--purge")
    assert tree(root) == before


def test_summary_when_not_set_up(make_ctx, capsys):
    from wgt import checks
    ctx = make_ctx()
    ops.state_unset(ctx, "WARP")
    checks.cmd_summary(ctx, [])
    assert "not set up (wgt setup)" in capsys.readouterr().out


# --- exit add, client rotate, restore's checks, wgcf install ---

PROTON = """[Interface]
PrivateKey = AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
Address = 10.2.0.2/32
DNS = 10.2.0.1

[Peer]
# NL#42
PublicKey = BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=
AllowedIPs = 0.0.0.0/0
Endpoint = 198.51.100.7:51820
"""


def test_exit_add_wires_a_new_city(make_ctx, tmp_path, monkeypatch):
    """exit add: the lowest free index (2: par-fr has 1, nyc-us 3), its own
    WARP account, the stored config with its label line, its routing live."""
    monkeypatch.setattr(host, "resolve4", lambda name: None)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    src = tmp_path / "nl.conf"
    src.write_text(PROTON)
    ctx = make_ctx(argv=["--label", "Amsterdam, NL"])
    wgcf = Path(ctx.paths.wgcf_bin)
    wgcf.write_text("#!/bin/sh\n")
    wgcf.chmod(0o755)
    ctx.runner = WgcfRunner(ctx.runner.answers, out=Out())
    ctx.files.runner = ctx.runner
    from wgt import exits
    exits.cmd_add(ctx, ["ams-nl", str(src)])
    e = ctx.snap.exit("ams-nl")
    assert (e.idx, e.label) == (2, "Amsterdam, NL")
    stored = Path(ctx.paths.jump_source("ams-nl")).read_text()
    assert stored.startswith("# exit ams-nl: Amsterdam, NL\n") and "Endpoint = 198.51.100.7:51820" in stored
    assert os.path.isfile(ctx.paths.px_conf("ams-nl")) and os.path.isfile(ctx.paths.wx_conf("ams-nl"))
    calls = "\n".join(ctx.runner.changes())
    assert "wgcf register" in calls and "wgcf generate" in calls
    assert f"ip rule add fwmark {22272 + 2} lookup {230 + 2}" in calls     # its WARP goes out via Proton


def test_rotate_replaces_the_key_live(make_ctx):
    from wgt import clients
    ctx = make_ctx(argv=["--endpoint", "203.0.113.9"])
    old = ctx.snap.client("mbp-1").pub
    clients.cmd_rotate(ctx, ["mbp-1"])
    new = ctx.snap.client("mbp-1")
    assert new.pub != old and new.ip == "10.2.10.2"
    calls = ctx.runner.changes()
    assert f"wg set wg0 peer {old} remove" in calls
    assert any(c.startswith(f"wg set wg0 peer {new.pub} preshared-key") for c in calls)
    assert "Endpoint = 203.0.113.9:" in Path(clients.client_conf(ctx, "mbp-1")).read_text()


def test_restore_checks_and_records(make_ctx, tmp_path):
    """A good archive passes check_archive; everything restored is recorded as
    NEW, so rollback won't treat a restored file as this server's original."""
    ctx = make_ctx()
    state, clients_db = ctx.paths.state, ctx.paths.client_db
    archive = tmp_path / "good.tar.gz"
    _tar(archive, [(state.lstrip("/"), "file"), (clients_db.lstrip("/"), "file")])
    members = backup.check_archive(str(archive), ctx.paths)
    assert state in members and clients_db in members
    backup.rebuild_backup_map(ctx, None, members)
    record = Path(ctx.paths.backup_map).read_text()
    assert f"{state}\tNEW" in record and f"{clients_db}\tNEW" in record


def test_install_wgcf_checks_the_download(make_ctx, monkeypatch):
    import hashlib
    import json

    from wgt import warp
    ctx = make_ctx(answers={"dpkg --print-architecture": (0, "amd64\n")})
    binary = b"#!/bin/sh\n"
    rel = {"assets": [{"browser_download_url": "https://x/wgcf_2.2_linux_amd64"},
                      {"browser_download_url": "https://x/checksums.txt"}]}

    def fetch(sums):
        return lambda url, timeout: {"https://x/wgcf_2.2_linux_amd64": binary, "https://x/checksums.txt": sums}.get(
            url, json.dumps(rel).encode())

    good = f"{hashlib.sha256(binary).hexdigest()}  wgcf_2.2_linux_amd64\n".encode()
    assert warp.install_wgcf(ctx, fetch=fetch(good))
    assert any(c.startswith("install -m 755 ") and c.endswith(ctx.paths.wgcf_bin) for c in ctx.runner.calls)
    with pytest.raises(WgtError, match="checksum mismatch"):
        warp.install_wgcf(ctx, reinstall=True, fetch=fetch(b"0" * 64 + b"  wgcf_2.2_linux_amd64\n"))
    # Checksums published, but none for this binary: unverified, so not installed either.
    ctx.runner.calls.clear()
    with pytest.raises(WgtError, match="don't list wgcf_2.2_linux_amd64"):
        warp.install_wgcf(ctx, reinstall=True, fetch=fetch(b"0" * 64 + b"  wgcf_2.2_linux_arm64\n"))
    assert not any(c.startswith("install -m 755 ") for c in ctx.runner.calls)


def test_drift_seen_when_wg0_runs_older_rules(make_ctx):
    """wg0 started with an older config: no WARP rule live, though wg0.conf has it."""
    from wgt import checks
    ctx = make_ctx(answers={"ip rule show": (0, "0:\tfrom all lookup local\n"),
                            "ip -6 rule show": (0, "0:\tfrom all lookup local\n")})
    drift = checks.Tests(ctx).wg0_drift()
    assert "ip WARP rule" in drift and "ip -6 WARP rule" in drift


def test_setup_restarts_wg0_only_on_drift(make_ctx, monkeypatch):
    from wgt import checks
    from wgt import server as srv
    ctx = make_ctx()
    monkeypatch.setattr(checks.Tests, "wg0_drift", lambda self: [])
    srv.restart_if_drifted(ctx)
    assert not any("restart wg-quick@wg0" in c for c in ctx.runner.changes())
    monkeypatch.setattr(checks.Tests, "wg0_drift", lambda self: ["ip WARP rule"])
    srv.restart_if_drifted(ctx)
    assert "systemctl restart wg-quick@wg0" in ctx.runner.changes()


def test_restore_passes_setting_flags_to_setup(make_ctx):
    from wgt import backup
    ctx = make_ctx(argv=["--endpoint", "vpn.example.com", "--port", "51821", "--location", "Paris"])
    flags = backup.setup_flags(ctx)
    assert flags[flags.index("--endpoint") + 1] == "vpn.example.com"
    assert flags[flags.index("--port") + 1] == "51821" and flags[-2:] == ["--location", "Paris"]


NEW_PROFILE = """[Interface]
PrivateKey = NEWWARPKEY=
Address = 172.16.0.2/32, 2606:4700:110:8a36::2/128
DNS = 1.1.1.1
MTU = 1280
[Peer]
PublicKey = bmXOC+F1FxEMF9dyiK2H5/1SUtzH0JuVo51h2wPfgyo=
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = 162.159.192.1:2408
"""


@pytest.fixture
def rotating(make_ctx, monkeypatch):
    """wgt warp rotate with wgcf faked: <refuse> makes registration fail."""
    from wgt import warp
    state = {"refuse": False}

    def fake_register(ctx, directory=None, regenerate=False, home=""):
        if state["refuse"]:
            return False
        os.makedirs(directory, exist_ok=True)
        Path(directory, "wgcf-account.toml").write_text("account = 'new'\n")
        Path(directory, "wgcf-profile.conf").write_text(NEW_PROFILE)
        return True
    monkeypatch.setattr(warp, "register", fake_register)
    monkeypatch.setattr(warp, "install_wgcf", lambda ctx, **kw: True)
    monkeypatch.setattr(exits.time, "sleep", lambda s: None)
    return state


def test_warp_rotate_host(make_ctx, rotating):
    ctx = make_ctx(argv=["warp", "rotate"], answers={"ip link show warp": (0, "")})
    Path(ctx.paths.wgcf_account()).write_text("account = 'old'\n")
    warp_rotate.cmd_warp(ctx, ["rotate"])
    assert Path(ctx.paths.wgcf_account()).read_text() == "account = 'new'\n"
    assert "NEWWARPKEY=" in Path(ctx.cfg.warp_conf).read_text()
    assert not os.path.exists(ctx.paths.wgcf_scratch(ctx.paths.wgcf_dir))
    assert any("restart wg-quick@warp" in c or "wg-quick down warp" in c for c in ctx.runner.calls)


def test_warp_rotate_refused_changes_nothing(make_ctx, rotating):
    rotating["refuse"] = True
    ctx = make_ctx()
    Path(ctx.paths.wgcf_account()).write_text("account = 'old'\n")
    with pytest.raises(WgtError, match="refused"):
        warp_rotate.cmd_warp(ctx, ["rotate"])
    assert Path(ctx.paths.wgcf_account()).read_text() == "account = 'old'\n"


def test_warp_rotate_unknown_jump(make_ctx, rotating):
    with pytest.raises(WgtError, match="no jump 'xx-yy'"):
        warp_rotate.cmd_warp(make_ctx(), ["rotate", "xx-yy"])


def test_with_device_network_adds_and_replaces_server_address():
    old = "# wireguard-tools client: a on s\n# exit: x\n[Interface]\nPrivateKey = K\n"
    want = "# wireguard-tools client: a on s\n# exit: x\n# device-network: 10.9.8.0/24\n[Interface]\nPrivateKey = K\n"
    assert render.with_device_network(old) == want
    assert render.with_device_network(want) == want
    swapped = old.replace("[Interface]", "# server-address: 10.8.0.1/32, fd00::1/128\n[Interface]")
    assert render.with_device_network(swapped) == want


# --- settings belong to setup; setup says when it's finished ---

@pytest.mark.parametrize("argv", [["client", "add", "zed", "--port", "9999"], ["jump", "list", "--dns", "9.9.9.9"],
                                  ["device", "list", "--allowed-ips", "10.0.0.0/8"]])
def test_settings_are_refused_outside_setup(argv, capsys):
    assert cli.main(argv) == 1
    assert "server setting" in capsys.readouterr().err


def test_only_setup_and_restore_read_settings_from_the_environment():
    from wgt.ctx import settings_env
    env = {"WG_PORT": "9999"}
    assert settings_env(cli.parse(["setup"]), "setup", env) == env
    assert settings_env(cli.parse(["restore", "x"]), "restore", env) == env
    assert settings_env(cli.parse(["client", "list"]), "client", env) == {}
    assert settings_env(cli.parse(["client", "add", "a", "--allowed-ips", "10.0.0.0/8"]), "client", env) == {}


def test_is_set_up_follows_the_state_file(make_ctx):
    ctx = make_ctx()
    assert ops.is_set_up(ctx)                     # WARP=on: an older version's marker
    ops.state_unset(ctx, "WARP")
    assert not ops.is_set_up(ctx)
    ops.state_set(ctx, "SETUP_DONE", "1")
    assert ops.is_set_up(ctx)


def test_debug_shows_everything_setup_writes(make_ctx, capsys):
    ctx = make_ctx()
    cli.command("debug")(ctx, [])
    out = capsys.readouterr().out
    for piece in ("=== wg0\n", "=== hooks-down\n", "=== wg-tools-device-watchdog.timer\n",
                  "=== wg-tools-warp-watchdog.service\n", "=== dropin\n", "=== sysctl\n", "=== packages\n"):
        assert piece in out


def test_debug_asks_for_accept_ra_as_setup_does(make_ctx, capsys):
    """A VLAN's dot would read as a separator: the same sysctl name both ways."""
    ctx = make_ctx()
    ops.state_set(ctx, "PUB_IF", "eth0.100")
    cli.command("debug")(ctx, ["system"])
    assert "sysctl -n net.ipv6.conf.eth0/100.accept_ra" in ctx.runner.calls


def test_backup_prunes_only_this_hosts_archives(make_ctx, tmp_path):
    ctx = make_ctx()
    dest = tmp_path / "shared"
    dest.mkdir()
    other = dest / "wireguard-tools.aaa.20200101-000000.tar.gz"
    other.write_text("another host's")
    mine = [dest / f"wireguard-tools.boba.202001{i + 10}-000000.tar.gz" for i in range(12)]
    for path in mine:
        path.write_text("old")
    backup.prune_archives(ctx, str(dest), "boba")
    assert other.exists()
    assert [p.exists() for p in mine] == [False, False] + [True] * 10


def test_restore_passes_setups_switches_on(make_ctx):
    ctx = make_ctx(argv=["restore", "x", "--force", "--new-server-keys", "--port", "1234"])
    flags = backup.setup_flags(ctx)
    assert "--force" in flags and "--new-server-keys" in flags and flags[flags.index("--port") + 1] == "1234"


def test_a_checksum_file_that_wont_download_stops_the_install(make_ctx):
    import json

    from wgt import warp
    ctx = make_ctx(answers={"dpkg --print-architecture": (0, "amd64\n")})
    rel = {"assets": [{"browser_download_url": "https://x/wgcf_2.2_linux_amd64"},
                      {"browser_download_url": "https://x/checksums.txt"}]}

    def fetch(url, timeout):
        if url.endswith("checksums.txt"):
            raise OSError("timed out")
        return b"#!/bin/sh\n" if url.endswith("amd64") else json.dumps(rel).encode()
    with pytest.raises(WgtError, match="checksums"):
        warp.install_wgcf(ctx, reinstall=True, fetch=fetch)
    assert not any(c.startswith("install -m 755 ") for c in ctx.runner.calls)


def test_wgt_test_notices_the_static_address_rule_missing(make_ctx):
    from wgt import checks
    answers = {"ip -4 -o addr show dev wg0": (0, "85: wg0 inet 10.9.8.1/24 scope global wg0\n"),
               "ip rule show": (0, "9300:\tfrom all to 10.9.8.0/24 iif wg0 unreachable\n")}
    assert checks.Tests(make_ctx(answers=answers)).device_net_ok()


def test_wgt_test_fails_without_the_static_address_rule(make_ctx):
    from wgt import checks
    answers = {"ip -4 -o addr show dev wg0": (0, "85: wg0 inet 10.9.8.1/24 scope global wg0\n"),
               "ip rule show": (0, "")}
    assert not checks.Tests(make_ctx(answers=answers)).device_net_ok()


# --- restore, and small things that reach running servers ---

def _archive(tmp_path, state, *others):
    path = tmp_path / "a.tar.gz"
    wg = tmp_path / "src"
    wg.mkdir(exist_ok=True)
    (wg / "wg-tools.state").write_text(state)
    with tarfile.open(path, "w:gz") as t:
        t.add(wg / "wg-tools.state", arcname=others[0] if others else "x")
    return str(path)


@pytest.mark.parametrize("state,old", [("WARP=on\n", True), ("WARP=on\nNAMES=3\n", False),
                                       ("SETUP_DONE=1\n", False), ("WG_PORT=1\n", False)])
def test_old_archives_are_told_apart(state, old):
    assert backup.old_name_meanings(state) is old


def _unset_up(ctx):
    ops.state_unset(ctx, "WARP")


def test_restore_refuses_old_name_meanings_unless_told(make_ctx, tmp_path, monkeypatch):
    ctx = make_ctx(argv=["restore"])
    _unset_up(ctx)
    for db in (ctx.paths.client_db, ctx.paths.exit_db, ctx.paths.device_db, ctx.paths.disabled_db):
        if os.path.exists(db):
            os.remove(db)
    ctx.reload()
    archive = _archive(tmp_path, "WARP=on\nWG_PORT=1\n", ctx.paths.state.lstrip("/"))
    with pytest.raises(WgtError, match="--old-names"):
        backup.cmd_restore(ctx, [archive])


def test_restore_refuses_to_mix_with_what_rollback_kept(make_ctx, tmp_path):
    ctx = make_ctx(argv=["restore"])
    _unset_up(ctx)
    archive = _archive(tmp_path, "SETUP_DONE=1\n", ctx.paths.state.lstrip("/"))
    with pytest.raises(WgtError, match="rollback --purge"):
        backup.cmd_restore(ctx, [archive])


def test_restore_refuses_to_mix_with_a_kept_routes_file(make_ctx, tmp_path):
    """exits.routes is a database like exits.db: one left by a rollback would mix in."""
    ctx = make_ctx(argv=["restore"])
    _unset_up(ctx)
    for db in (ctx.paths.client_db, ctx.paths.exit_db, ctx.paths.device_db, ctx.paths.disabled_db):
        if os.path.exists(db):
            os.remove(db)
    Path(ctx.paths.exit_routes_db).write_text("nyc-us 10.20.30.0/24\n")
    ctx.reload()
    archive = _archive(tmp_path, "SETUP_DONE=1\n", ctx.paths.state.lstrip("/"))
    with pytest.raises(WgtError, match="exits.routes would mix"):
        backup.cmd_restore(ctx, [archive])


def test_a_routed_subnet_is_refused_and_the_saved_one_isnt(make_ctx):
    ctx = make_ctx(argv=["--subnet", "192.0.2.0/25"])
    ctx.facts.routes4 = "default via 192.0.2.1 dev eth0\n192.0.2.0/24 dev eth0 scope link\n10.8.0.0/24 dev wg0\n"
    with pytest.raises(WgtError, match="already routes"):
        srv.refuse_routed_subnet(ctx)
    ctx = make_ctx(argv=["--subnet", "10.8.0.0/24"])
    ctx.facts.routes4 = "10.8.0.0/24 dev wg0 scope link\n"
    srv.refuse_routed_subnet(ctx)


def test_rollback_puts_the_hosts_file_back(make_ctx, tmp_path, monkeypatch):
    ctx = make_ctx()
    hosts = Path(ctx.paths.hosts_file)
    hosts.write_text("127.0.0.1 localhost\n127.0.1.1 oldname\n")
    ctx.files.write(str(hosts), 0o644, "127.0.0.1 localhost\n127.0.1.1 dengar\n")
    srv._restore_host_files(ctx)
    assert "oldname" in hosts.read_text()


def test_rollback_puts_back_the_hostname_setup_set(make_ctx):
    ctx = make_ctx()
    ops.state_set(ctx, "ORIG_HOSTNAME", "racknerd-1a2b")
    ops.state_set(ctx, "SET_HOSTNAME", "boba")   # make_ctx's host is boba
    srv._restore_host_files(ctx)
    assert "hostnamectl set-hostname racknerd-1a2b" in ctx.runner.calls
    assert not store.state_get(ctx.snap.state, "ORIG_HOSTNAME")
    assert not store.state_get(ctx.snap.state, "SET_HOSTNAME")


def test_rollback_leaves_a_hostname_someone_else_set(make_ctx):
    ctx = make_ctx()
    ops.state_set(ctx, "ORIG_HOSTNAME", "racknerd-1a2b")
    ops.state_set(ctx, "SET_HOSTNAME", "dengar")   # renamed boba since, by hand
    srv._restore_host_files(ctx)
    assert not any("hostnamectl" in c for c in ctx.runner.calls)
    assert not store.state_get(ctx.snap.state, "ORIG_HOSTNAME")


def test_the_rename_is_recorded_before_it_happens_and_keeps_the_first_name(make_ctx, monkeypatch):
    ctx = make_ctx(argv=["--name", "dengar"])
    ctx.args.yes = False
    monkeypatch.setattr(ui, "have_tty", lambda: True)
    monkeypatch.setattr(ui, "yes", lambda *a, **k: True)
    ops.state_set(ctx, "ORIG_HOSTNAME", "racknerd-1a2b")   # an earlier rename's...
    ops.state_set(ctx, "SET_HOSTNAME", "boba")             # ...to the name the host still has
    ctx.runner.answers["hostnamectl set-hostname dengar"] = (1, "")   # stopped there
    with pytest.raises(CommandFailed):
        srv.offer_hostname(ctx)
    state = store.read_text(ctx.paths.state)
    assert store.state_get(state, "ORIG_HOSTNAME") == "racknerd-1a2b"
    assert store.state_get(state, "SET_HOSTNAME") == "dengar"


def test_a_rename_after_a_hand_rename_goes_back_to_the_hand_given_name(make_ctx, monkeypatch):
    ctx = make_ctx(argv=["--name", "dengar"])
    ctx.args.yes = False
    monkeypatch.setattr(ui, "have_tty", lambda: True)
    monkeypatch.setattr(ui, "yes", lambda *a, **k: True)
    ops.state_set(ctx, "ORIG_HOSTNAME", "racknerd-1a2b")
    ops.state_set(ctx, "SET_HOSTNAME", "ig88")   # setup's, but the host is boba now: renamed by hand
    srv.offer_hostname(ctx)
    assert store.state_get(ctx.snap.state, "ORIG_HOSTNAME") == "boba"


def test_a_busy_lock_after_a_yes_says_nothing_was_done(make_ctx, monkeypatch):
    ctx = make_ctx()

    def busy(c):
        raise WgtError("another wireguard-tools command is running")
    monkeypatch.setattr(ops, "take_lock", busy)
    with pytest.raises(WgtError, match="running: nothing was done, run it again"):
        ops.lock_as_seen(ctx)


def test_a_newline_cant_become_another_saved_key():
    with pytest.raises(WgtError):
        store.state_set("", "LOCATION", "Paris\nWG_PORT=9")


def test_a_value_is_never_read_as_a_template_key():
    out = render.render_template("watchdog.timer.tmpl", WHAT="{{EVERY}} {{NOPE}}", BOOT="1", EVERY="2", ACCURACY="3")
    assert "Run the wireguard-tools {{EVERY}} {{NOPE}} watchdog every 2s" in out
    with pytest.raises(WgtError, match="no value for {{ACCURACY}}"):
        render.render_template("watchdog.timer.tmpl", WHAT="x", BOOT="1", EVERY="2")


def test_a_template_key_may_hold_digits(tmp_path, monkeypatch):
    (tmp_path / "t.tmpl").write_text("Address = {{SUBNET6}}\n")
    monkeypatch.setattr(render, "TEMPLATE_DIR", str(tmp_path))
    assert render.render_template("t.tmpl", SUBNET6="fd00::/64") == "Address = fd00::/64\n"
    with pytest.raises(WgtError, match="no value for {{SUBNET6}}"):
        render.render_template("t.tmpl")


def test_a_label_with_a_backslash_survives_a_rename(make_ctx):
    from wgt import clients
    ctx = make_ctx()
    ops.save_exit(ctx, store.Exit("par-fr", ctx.snap.exit("par-fr").idx, r"Par\is"))
    name = "warp.par-fr.mbp-1"
    conf = Path(clients.client_conf(ctx, name))
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text("# exit: x\n[Interface]\n")
    Path(ctx.paths.psk(name)).parent.mkdir(parents=True, exist_ok=True)
    Path(ctx.paths.psk(name)).write_text("PSK\n")
    clients.cmd_rename(ctx, [name, "warp.par-fr.mbp-2"])
    assert r"Par\is" in Path(clients.client_conf(ctx, "warp.par-fr.mbp-2")).read_text()


def test_a_terminal_that_goes_away_doesnt_stop_a_command():
    class Gone:
        def write(self, text):
            raise BrokenPipeError

        def flush(self):
            pass
    from wgt.system import Out
    out = Out(Gone(), Gone())
    out.info("still here")
    out.error("still here")


def test_warp_up_without_its_config_is_a_restart_asked_before_the_write(make_ctx):
    # Asked after it, a no would leave a re-run an unchanged file and never a restart.
    from wgt import units
    missing = str(make_ctx().paths.wg("nowhere.conf"))
    assert units.restart_due(make_ctx(), "warp", missing, "[Interface]\nMTU = 1280\n")
    down = make_ctx(answers={"ip link show dev warp": (1, "")})
    assert not units.restart_due(down, "warp", missing, "[Interface]\nMTU = 1280\n")


@pytest.mark.parametrize("endpoint, is_ip", [("1.2.3.4", True), ("[2001:db8::1]", True), ("2001:db8::1", True),
                                             ("cafe.de", False), ("dead.beef", False), ("vpn.example.com", False)])
def test_restore_only_forgets_an_endpoint_that_is_an_address(endpoint, is_ip):
    from wgt import backup
    assert backup._is_ip(endpoint) is is_ip


def test_a_vlans_sysctl_name_keeps_its_dot_out_of_the_path():
    from wgt import render
    assert render.accept_ra_key("eth0") == "net.ipv6.conf.eth0.accept_ra"
    assert render.accept_ra_key("eth0.100") == "net.ipv6.conf.eth0/100.accept_ra"



def test_the_devices_static_network_routed_elsewhere_is_refused(make_ctx):
    # wg0 takes 10.9.8.1/24 whatever its subnet: a host already routing it (a VPC) would clash
    ctx = make_ctx(answers={"ip link show dev wg0": (1, "")})   # wg0 not up yet
    ctx.facts.routes4 = "default via 192.0.2.1 dev eth0\n10.9.8.0/24 dev eth1 scope link\n"
    with pytest.raises(WgtError, match="static addresses"):
        srv.refuse_routed_subnet(ctx)


@pytest.mark.parametrize("assets,want", [
    (["checksums.txt", "wgcf_2.3.1_darwin_amd64", "wgcf_2.3.1_linux_amd64"], "checksums.txt"),   # wgcf v2.3.1
    (["wgcf_x_darwin_amd64.sha256", "checksums.txt", "wgcf_x_linux_amd64", "wgcf_x_linux_amd64.sha256"],
     "wgcf_x_linux_amd64.sha256"),          # its own first, not another platform's that comes first
    (["wgcf_x_darwin_amd64.sha256", "checksums.txt", "wgcf_x_linux_amd64"], "checksums.txt"),
])
def test_the_checksums_picked_are_the_ones_for_this_binary(assets, want):
    from wgt import warp
    urls = [f"https://h/d/{a}" for a in assets]
    assert warp._sums_url(urls, next(u for u in urls if u.endswith("linux_amd64"))) == f"https://h/d/{want}"


def test_a_binarys_own_sha256_may_hold_the_hash_alone(monkeypatch):
    from wgt import warp
    monkeypatch.delenv("WGCF_SHA256", raising=False)
    fetch = lambda url, timeout: b"abc123\n"   # noqa: E731
    assert warp._published_sha256(fetch, "https://h/d/wgcf_x_linux_amd64.sha256", "wgcf_x_linux_amd64") == "abc123"


def test_settings_changed_while_waiting_for_the_lock_stop_the_command(make_ctx):
    from wgt import system
    ctx, watchdog_ctx = make_ctx(), make_ctx(bare=True)   # both loaded before setup changed the port
    system.write_private(ctx.paths.state, store.state_set(store.read_text(ctx.paths.state), "WG_PORT", "51999"))
    with pytest.raises(WgtError, match="settings changed"):
        ops.take_lock(ctx)
    assert ops.try_lock(watchdog_ctx) is False   # a watchdog skips the run: the next one loads them
    fresh = make_ctx()
    assert ops.try_lock(fresh) is True           # and the refusal let the lock go
    ops.release_lock(fresh)


def test_the_devices_static_network_is_checked_with_wg0_up_too(make_ctx):
    # a wg0 started before static addresses existed doesn't hold 10.9.8.1 yet
    ctx = make_ctx()   # wg0 up
    ctx.facts.routes4 = "default via 192.0.2.1 dev eth0\n10.9.8.0/24 dev eth1 scope link\n"
    with pytest.raises(WgtError, match="static addresses"):
        srv.refuse_routed_subnet(ctx)
    ctx.facts.routes4 = "10.9.8.0/24 dev wg0 proto kernel scope link src 10.9.8.1\nlocal 10.9.8.1 dev wg0 table local\n"
    srv.refuse_routed_subnet(ctx)   # wg0's own


@pytest.mark.parametrize("answer", ['{"city": null}', "[]", '"Paris"', '{"city": " Atlanta\\n GA "}'])
def test_a_location_lookup_saves_a_city_or_nothing(make_ctx, answer):
    ctx = make_ctx(answers={"curl -fsS --max-time 8 https://ipinfo.io/json": (0, answer)})
    ops.state_unset(ctx, "LOCATION")
    srv.save_location(ctx)
    assert store.state_get(ctx.snap.state, "LOCATION") == ("Atlanta GA" if "Atlanta" in answer else "")


def test_dns_on_the_servers_own_tunnel_address_is_named(make_ctx):
    """WGT-SERVER refuses devices outside the server group there: DNS on it fails for them."""
    ctx = make_ctx()
    assert srv.dns_on_server(ctx) == []
    for own in (ctx.cfg.server_ip, "10.9.8.1", f"{ctx.cfg.p6}::1"):
        ctx.cfg.values["WG_DNS"] = f"1.1.1.1, {own}, home.lan"
        assert srv.dns_on_server(ctx) == [own]
