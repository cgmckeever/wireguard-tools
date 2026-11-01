"""wgt-ctl's local parts: the local-file comment, the server list, the
details table and the first-word grammar."""

from __future__ import annotations

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
