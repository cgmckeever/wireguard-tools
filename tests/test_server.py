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
