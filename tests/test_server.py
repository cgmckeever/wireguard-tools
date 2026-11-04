"""server.py commands that can be checked without a real server."""

from __future__ import annotations

import io
import os
import shutil
import tarfile
from pathlib import Path

import pytest
from conftest import FIXTURES, KEYS

from wgt import backup, cli, host, ops
from wgt import server as srv
from wgt.const import Paths
from wgt.errors import WgtError
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
    ctx.cfg.saved["WARP"] = ""     # "not set up"
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
    assert srv.stop_before_rewrite(ctx) is False            # wg0 is up, but only a comment differs
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
    assert srv.ensure_server_keys(ctx) is False
    assert cli.parse(["setup", "--new-server-keys"]).new_server_keys


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
    paths = Paths(wg_dir=str(root / "wg"), backup_dir=str(root / "backups"), lock_file=str(tmp_path / "lock"),
                  run_dir=str(root / "run"), systemd_dir=str(root / "systemd"), sbin_dir=str(root / "sbin"),
                  sysctl_file=str(root / "99-wg.conf"), wgcf_bin=str(wgcf), hosts_file=str(root / "hosts"),
                  cloud_cfg_dir=str(root / "cloud"))
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
    run("rollback", "--purge")
    assert tree(root) == before


def test_summary_when_not_set_up(make_ctx, capsys):
    from wgt import checks
    ctx = make_ctx()
    ctx.cfg.saved.pop("WARP", None)
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
