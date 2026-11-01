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


def test_restore_refuses_unsafe_archives(make_ctx, tmp_path):  # noqa: F811
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


def test_restore_needs_a_backup(make_ctx, tmp_path):  # noqa: F811
    ctx = make_ctx()
    a = str(tmp_path / "x.tgz")
    _tar(a, [("etc/other", "file")])
    with pytest.raises(WgtError, match="not a wireguard-tools backup"):
        backup.cmd_restore(ctx, [a])


def test_comment_only_change_needs_no_restart(make_ctx):  # noqa: F811
    ctx = make_ctx()
    from wgt import ops
    ops.write_wg_conf(ctx)
    text = Path(ctx.cfg.wg_conf).read_text()
    Path(ctx.cfg.wg_conf).write_text(text.replace("(use wgt client)", "(use wg-client.sh)"))
    assert srv.stop_before_rewrite(ctx) is False            # wg0 is up, but only a comment differs
    assert ops.write_wg_conf(ctx) is False                  # rewritten, settings unchanged
    assert "(use wgt client)" in Path(ctx.cfg.wg_conf).read_text()
    assert not any("systemctl stop" in c for c in ctx.runner.changes())


def test_dry_run_remove_shows_wg0_diff(make_ctx, capsys):  # noqa: F811
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


def test_force_keeps_server_keys(make_ctx):  # noqa: F811
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
