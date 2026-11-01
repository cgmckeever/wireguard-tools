"""server.py commands that can be checked without a real server."""

from __future__ import annotations

import io
import os
import tarfile

import pytest
from test_clients import server  # noqa: F401  (the fixture)

from wgt import server as srv
from wgt.errors import WgtError


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


def test_restore_refuses_unsafe_archives(server, tmp_path):  # noqa: F811
    ctx = server()
    ctx.cfg.saved["WARP"] = ""     # "not set up"
    state = ctx.paths.state.lstrip("/")
    bad = [[(state, "file"), (ctx.paths.wg("..", "..", "root", "x").lstrip("/"), "file")],
           [(state, "file"), (ctx.paths.wg("link").lstrip("/"), "sym")]]
    for i, members in enumerate(bad):
        a = str(tmp_path / f"a{i}.tgz")
        _tar(a, members)
        with pytest.raises(WgtError, match="unsafe entry"):
            srv.cmd_restore(ctx, [a])


def test_restore_needs_a_backup(server, tmp_path):  # noqa: F811
    ctx = server()
    a = str(tmp_path / "x.tgz")
    _tar(a, [("etc/other", "file")])
    with pytest.raises(WgtError, match="not a wireguard-tools backup"):
        srv.cmd_restore(ctx, [a])


def test_comment_only_change_needs_no_restart(server):  # noqa: F811
    ctx = server()
    from wgt import ops
    ops.write_wg_conf(ctx)
    text = open(ctx.cfg.wg_conf).read()
    open(ctx.cfg.wg_conf, "w").write(text.replace("(use wgt client)", "(use wg-client.sh)"))
    assert srv.stop_before_rewrite(ctx) is False            # wg0 is up, but only a comment differs
    assert ops.write_wg_conf(ctx) is False                  # rewritten, settings unchanged
    assert "(use wgt client)" in open(ctx.cfg.wg_conf).read()
    assert not any("systemctl stop" in c for c in ctx.runner.changes())


def test_dry_run_remove_shows_wg0_diff(server, capsys):  # noqa: F811
    from wgt import clients, ops
    ctx = server()
    ops.write_wg_conf(ctx)
    capsys.readouterr()
    before = {p: open(p).read() for p in (ctx.cfg.wg_conf, ctx.paths.client_db)}
    ctx.runner.dry_run = True
    clients.cmd_remove(ctx, ["mbp-1"])
    printed = capsys.readouterr().out
    assert "-# mbp-1" in printed                             # wg0.conf diff shows the peer going
    assert all(open(p).read() == t for p, t in before.items())


def test_force_keeps_server_keys(server):  # noqa: F811
    from wgt import cli
    ctx = server(argv=["--force"])
    for p in (ctx.paths.server_key, ctx.paths.server_pub):
        open(p, "w").write("KEY\n")
    assert srv.ensure_server_keys(ctx) is False
    assert cli.parse(["setup", "--new-server-keys"]).new_server_keys
