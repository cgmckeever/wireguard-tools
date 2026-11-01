"""wgt-ctl's local parts: the local-file comment, the server list, the
details table and the first-word grammar."""

from __future__ import annotations

import os

import pytest

from wgt import ctl
from wgt.errors import WgtError


def test_local_file_comment_replaces_old_one():
    text = "# local file: old.conf\n[Interface]\nPrivateKey = K\n"
    out = ctl.with_local_file_comment(text, "paris.france.conf")
    assert out == "# local file: paris.france.conf\n[Interface]\nPrivateKey = K\n"
    assert ctl.with_local_file_comment(out, "paris.france.conf") == out


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setattr(ctl, "LOCAL", str(tmp_path))
    monkeypatch.setattr(ctl, "LIST", str(tmp_path / "servers"))
    monkeypatch.setattr(ctl, "LINKS", str(tmp_path / "details.tsv"))
    (tmp_path / "servers").write_text("# comment\ndengar  root@1.2.3.4\nboba root@5.6.7.8 /srv/wgt\n")
    (tmp_path / "details.tsv").write_text("boba\tparis.france.conf\tpar-fr\tParis, France\n")
    return tmp_path


def test_servers_and_find(local):
    assert [(s.name, s.target, s.dir) for s in ctl.servers()] == [
        ("dengar", "root@1.2.3.4", ctl.DEF_DIR), ("boba", "root@5.6.7.8", "/srv/wgt")]
    assert ctl.find("2").name == "boba"
    with pytest.raises(WgtError):
        ctl.find("nope")


def test_link_of(local):
    assert ctl.link_of("boba", "paris.france.conf") == ("par-fr", "Paris, France")
    assert ctl.link_of("dengar", "paris.france.conf") is None


def test_reserved_names():
    assert not ctl.valid_server("all") and not ctl.valid_server("init") and ctl.valid_server("boba")


def test_all_refuses_single_server_commands(local):
    with pytest.raises(WgtError, match="takes one server"):
        ctl.on("all", "ssh", [])


@pytest.mark.parametrize("line", ["onlyname", "2bad root@h", "ok -oProxyCommand=x", "ok root@h relative/dir",
                                  "all root@h"])
def test_bad_server_lines_refused(local, line):
    (local / "servers").write_text(line + "\n")
    with pytest.raises(WgtError, match="line 1"):
        ctl.servers()


def test_dispatch(local, monkeypatch):
    seen = []
    monkeypatch.setattr(ctl, "do_wgt", lambda s, args: seen.append((s.name, args)) or 0)
    assert ctl.main(["boba", "status"]) == 0
    assert ctl.main(["2", "client", "list"]) == 0
    assert seen == [("boba", ["status"]), ("boba", ["client", "list"])]
    assert ctl.main(["bobaa", "status"]) == 1          # a typo is not a command
    assert ctl.main(["boba", "frobnicate"]) == 1
    assert ctl.main(["boba", "pull", "--dry-run"]) == 1   # ctl commands take no stray arguments


def test_all_keeps_going(local, monkeypatch):
    def fake(s, args):
        if s.name == "dengar":
            raise WgtError("boom")
        return 0
    monkeypatch.setattr(ctl, "do_wgt", fake)
    assert ctl.on("all", "test", []) == 1


def test_all_log_does_not_follow(local, monkeypatch):
    seen = []
    monkeypatch.setattr(ctl, "do_wgt", lambda s, args: seen.append(args) or 0)
    ctl.on("all", "log", [])
    assert all("--no-follow" in a for a in seen)


def test_servers_sh_wrapper_translates(tmp_path):
    import shutil
    import subprocess
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "bin").mkdir()
    shutil.copy(os.path.join(ctl.ROOT, "scripts", "servers.sh"), root / "scripts")
    (root / "bin" / "wgt-ctl").write_text('#!/bin/sh\nprintf "%s|" "$@"\n')
    os.chmod(root / "bin" / "wgt-ctl", 0o755)

    def run(*args):
        return subprocess.run(["bash", str(root / "scripts" / "servers.sh"), *args],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout
    assert run("status", "boba") == "boba|status|"
    assert run("client", "boba", "add", "x") == "boba|client|add|x|"
    assert run("status") == "status|"
    assert run("clients") == "clients|"
