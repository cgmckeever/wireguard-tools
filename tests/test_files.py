"""system.Files (writes with backups) and the store's robustness."""

from __future__ import annotations

import os

import pytest

from wgt import store
from wgt.const import Paths
from wgt.errors import WgtError
from wgt.system import FakeRunner, Files, Out, is_copy_of


@pytest.fixture
def files(tmp_path):
    def make(dry_run=False):
        paths = Paths(wg_dir=str(tmp_path / "wg"), backup_dir=str(tmp_path / "bak"))
        os.makedirs(paths.wg_dir, exist_ok=True)
        return Files(paths, FakeRunner(dry_run=dry_run, out=Out()), clock=iter(range(1, 10 ** 6)).__next__)
    return make


def test_write_unchanged_changed_and_mode(files):
    f = files()
    p = f.paths.wg("x.conf")
    assert f.write(p, 0o600, "a\n") is True
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    assert f.write(p, 0o600, "a\n") is False
    assert f.write(p, 0o644, "b\n") is True
    assert store.backup_orig(store.read_text(f.paths.backup_map), p) == "NEW"


def test_hand_made_bak_survives(files):
    f = files()
    p = f.paths.wg("wg0.conf")
    open(p, "w").write("old\n")
    open(p + ".bak", "w").write("mine\n")
    f.write(p, 0o600, "new\n")
    f.write(p, 0o600, "newer\n")
    assert open(p + ".bak").read() == "mine\n"
    assert not is_copy_of("wg0.conf.bak", "wg0.conf") and is_copy_of("wg0.conf.20260101-000000.bak", "wg0.conf")


def test_prune_keeps_original_and_five_newest(files):
    f = files()
    p = f.paths.wg("c.conf")
    open(p, "w").write("0\n")
    for i in range(1, 10):
        f.write(p, 0o600, f"{i}\n")
    d = f.change_dir(p)
    copies = sorted(n for n in os.listdir(d) if is_copy_of(n, "c.conf"))
    orig = store.backup_orig(store.read_text(f.paths.backup_map), p)
    assert os.path.basename(orig) in copies and len(copies) == 6
    assert open(orig).read() == "0\n"


def test_dry_run_hides_secret_files(files, capsys):
    f = files(dry_run=True)
    psk = os.path.join(f.paths.psk_dir, "phone.psk")
    f.write(psk, 0o600, "SECRETPSKVALUE=\n")
    f.write(f.paths.wg("c.conf"), 0o600, "PrivateKey = SECRETPRIV=\nAddress = 1\n")
    printed = capsys.readouterr().out
    assert "SECRETPSKVALUE" not in printed and "SECRETPRIV" not in printed
    assert not os.path.exists(psk)


def test_forget_removes_copies_and_record(files):
    f = files()
    p = f.paths.wg("gone.conf")
    f.write(p, 0o600, "1\n")
    f.write(p, 0o600, "2\n")
    f.forget(p)
    assert not os.path.exists(p)
    assert not [n for n in os.listdir(f.change_dir(p)) if is_copy_of(n, "gone.conf")]
    assert store.backup_orig(store.read_text(f.paths.backup_map), p) == ""


@pytest.mark.parametrize("line", ["par-fr", "par-fr x Paris", "par-fr 0 Paris", "par-fr 24 Paris", "x 1 bad"])
def test_bad_exits_db_lines_refused(line):
    with pytest.raises(WgtError, match="exits.db"):
        store.parse_exits(line + "\n")


@pytest.mark.parametrize("line", ["phone", "phone notanip KEY", "phone 10.0.0.2"])
def test_bad_clients_db_lines_refused(line):
    with pytest.raises(WgtError, match="clients.db"):
        store.parse_clients(line + "\n")


def test_db_tolerates_tabs_and_crlf():
    c = store.parse_clients("a\t10.0.0.2\tK\t0.0.0.0/0\r\n")[0]
    assert (c.name, c.ip, c.pub, c.allowed) == ("a", "10.0.0.2", "K", "0.0.0.0/0")


def test_read_text_keeps_bytes(tmp_path):
    p = tmp_path / "x"
    p.write_bytes(b"caf\xe9\r\n")
    assert store.read_text(str(p)).endswith("\r\n")
