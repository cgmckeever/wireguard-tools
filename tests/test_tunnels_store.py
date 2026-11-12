"""Client configs delivered to ~/wg-tunnels/devices/<device>/config/ (wgt/ctl/tunnels.py):
the server's copy wins over a copy of the same config, and nothing else is touched."""

from __future__ import annotations

import io
import os

import pytest

from wgt import ctl
from wgt.ctl import remote, tunnels
from wgt.errors import WgtError


def conf(config: str, key: str, server: str = "dengar", body: str = "") -> str:
    return (f"# wireguard-tools client: {config} on {server}\n[Interface]\nPrivateKey = {key}\n"
            f"Address = 10.0.0.2/32\n{body}")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("WG_TUNNELS", str(tmp_path / "wg-tunnels"))
    return tmp_path / "wg-tunnels"


def put(store, device: str, file: str, text: str):
    d = store / "devices" / device / "config"
    d.mkdir(parents=True, exist_ok=True)
    (d / file).write_text(text)
    return d / file


def kinds(actions):
    return [(a.kind, os.path.basename(a.path), a.config) for a in actions]


def run(store, issued, complete=True):
    devices = tunnels.all_devices()
    actions = tunnels.plan("dengar", issued, tunnels.local_files(devices), complete)
    tunnels.apply(actions)
    return actions


def test_a_new_config_gets_the_servers_name_and_private_folders(store):
    actions = run(store, {"warp.iphone": conf("warp.iphone", "K1")})
    assert kinds(actions) == [("new", "dengar.warp.iphone.conf", "warp.iphone")]
    path = store / "devices" / "iphone" / "config" / "dengar.warp.iphone.conf"
    assert path.read_text() == conf("warp.iphone", "K1")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    for d in (store, store / "devices", store / "devices" / "iphone", path.parent):
        assert oct(d.stat().st_mode & 0o777) == "0o700"


def test_a_copy_under_another_name_is_matched_by_key_and_keeps_its_name(store):
    old = put(store, "iphone", "dengar.chicago.iphone.conf", conf("chi-us.iphone", "K1", body="MTU = 1280\n"))
    actions = run(store, {"chi-us.iphone": conf("chi-us.iphone", "K1")})
    assert kinds(actions) == [("update", "dengar.chicago.iphone.conf", "chi-us.iphone")]
    assert old.read_text() == conf("chi-us.iphone", "K1")
    baks = [f for f in os.listdir(old.parent) if f.endswith(".bak")]
    assert len(baks) == 1 and baks[0].startswith("dengar.chicago.iphone.conf.")
    assert "MTU = 1280" in (old.parent / baks[0]).read_text()


def test_an_identical_copy_is_left_alone(store):
    put(store, "iphone", "mine.conf", conf("warp.iphone", "K1"))
    actions = run(store, {"warp.iphone": conf("warp.iphone", "K1")})
    assert kinds(actions) == [("same", "mine.conf", "warp.iphone")]
    assert os.listdir(store / "devices" / "iphone" / "config") == ["mine.conf"]


def test_a_renamed_config_keeps_its_keys_so_its_copy_follows(store):
    path = put(store, "iphone", "dengar.chi-us.iphone.conf", conf("chi-us.iphone", "K1"))
    actions = run(store, {"warp.chi-us.iphone": conf("warp.chi-us.iphone", "K1")})
    assert kinds(actions) == [("update", "dengar.chi-us.iphone.conf", "warp.chi-us.iphone")]
    assert path.read_text() == conf("warp.chi-us.iphone", "K1")


def test_a_rotated_config_is_matched_by_its_header(store):
    path = put(store, "iphone", "phone.conf", conf("warp.iphone", "OLD"))
    actions = run(store, {"warp.iphone": conf("warp.iphone", "NEW")})
    assert kinds(actions) == [("update", "phone.conf", "warp.iphone")]
    assert "PrivateKey = NEW" in path.read_text()


def test_a_header_from_another_server_is_not_this_config(store):
    put(store, "iphone", "ig88.warp.iphone.conf", conf("warp.iphone", "OTHER", server="ig88"))
    actions = run(store, {"warp.iphone": conf("warp.iphone", "K1")})
    assert kinds(actions) == [("new", "dengar.warp.iphone.conf", "warp.iphone")]


def test_a_file_that_isnt_a_copy_is_never_overwritten(store):
    path = put(store, "iphone", "dengar.warp.iphone.conf", "[Interface]\nPrivateKey = HAND\n")
    actions = run(store, {"warp.iphone": conf("warp.iphone", "K1")})
    assert kinds(actions) == [("refuse", "dengar.warp.iphone.conf", "warp.iphone")]
    assert path.read_text() == "[Interface]\nPrivateKey = HAND\n"


def test_a_copy_the_server_no_longer_has_retires_to_bak(store):
    gone = put(store, "iphone", "dengar.old.iphone.conf", conf("old.iphone", "GONE"))
    put(store, "iphone", "ig88.warp.iphone.conf", conf("warp.iphone", "IG", server="ig88"))
    put(store, "iphone", "proton.conf", "[Interface]\nPrivateKey = P\n")
    actions = run(store, {})
    assert kinds(actions) == [("retire", "dengar.old.iphone.conf", "old.iphone")]
    assert not gone.exists()
    assert sorted(f.endswith(".bak") for f in os.listdir(gone.parent)) == [False, False, True]


def test_nothing_retires_unless_the_list_is_complete(store):
    put(store, "iphone", "dengar.old.iphone.conf", conf("old.iphone", "GONE"))
    assert run(store, {}, complete=False) == []


def test_bak_files_are_not_copies(store):
    put(store, "iphone", "x.conf.20261112-120000.bak", conf("warp.iphone", "K1"))
    assert kinds(run(store, {"warp.iphone": conf("warp.iphone", "K1")})) == [
        ("new", "dengar.warp.iphone.conf", "warp.iphone")]


def test_a_renamed_devices_copy_stays_put_with_a_note(store):
    put(store, "phone", "dengar.warp.phone.conf", conf("warp.phone", "K1"))
    [a] = run(store, {"warp.iphone": conf("warp.iphone", "K1")})
    assert (a.kind, a.path.split(os.sep)[-3]) == ("update", "phone")
    assert "now 'iphone'" in a.note


def test_fetch_reads_every_config_by_its_header(store, monkeypatch):
    m = tunnels.MARK
    text = f"{m}dengar.warp.iphone.conf\n{conf('warp.iphone', 'K1')}{m}stray.conf\n[Interface]\n"
    monkeypatch.setattr(remote, "ssh", lambda target, command, **k: (0, text))
    assert tunnels.fetch(remote.Server("dengar", "root@h")) == {"warp.iphone": conf("warp.iphone", "K1")}
    monkeypatch.setattr(remote, "ssh", lambda target, command, **k: (255, ""))
    with pytest.raises(WgtError):
        tunnels.fetch(remote.Server("dengar", "root@h"))


def test_the_name_in_the_configs_is_the_servers_own(store):
    issued = {"a.x": conf("a.x", "K", server="crash")}
    assert tunnels.server_name(issued, remote.Server("boba", "root@h")) == "crash"
    assert tunnels.server_name({}, remote.Server("boba", "root@h")) == "boba"


@pytest.mark.parametrize("cmd,args,delivers", [
    ("client", ["add", "phone"], True), ("client", ["rotate", "warp.phone"], True),
    ("client", ["rename", "a.phone", "b.phone"], True), ("client", ["remove", "a.phone"], True),
    ("device", ["rename", "phone", "iphone"], True), ("client", ["add", "phone", "--dry-run"], False),
    ("client", ["list"], True), ("client", [], True), ("client", ["list", "--tsv"], False),
    ("client", ["disable", "a.phone"], False), ("device", ["set", "phone"], False),
])
def test_which_commands_deliver(cmd, args, delivers):
    assert tunnels.changes_configs(cmd, args) is delivers


def test_a_failed_delivery_warns_and_doesnt_fail_the_command(store, monkeypatch):
    def boom(s):
        raise WgtError("unreachable")
    told = io.StringIO()
    monkeypatch.setattr(remote.out, "err", told)
    monkeypatch.setattr(tunnels, "deliver", boom)
    tunnels.after(remote.Server("dengar", "root@h"), "client", ["add", "phone"])
    assert "sync retries" in told.getvalue()


def test_a_client_command_delivers_only_when_it_worked(store, monkeypatch):
    done = []
    monkeypatch.setattr(tunnels, "deliver", lambda s: done.append(s.name))
    s = remote.Server("dengar", "root@h")
    monkeypatch.setattr(remote, "do_wgt", lambda s, args: 1)
    assert ctl._run(s, "client", ["rotate", "warp.phone"]) == 1 and done == []
    monkeypatch.setattr(remote, "do_wgt", lambda s, args: 0)
    assert ctl._run(s, "client", ["rotate", "warp.phone"]) == 0 and done == ["dengar"]


def test_sync_takes_no_arguments(store):
    with pytest.raises(WgtError):
        tunnels.do_sync(remote.Server("dengar", "root@h"), ["extra"])


def test_a_servers_anchor_is_made_by_sync_even_with_nothing_to_stage(store, monkeypatch):
    monkeypatch.setattr(remote, "ssh", lambda target, command, **k: (0, ""))
    assert tunnels.deliver(remote.Server("ig88", "root@h")) == 0
    d = store / "servers" / "ig88" / "carriers"
    assert d.is_dir() and list(d.iterdir()) == []
    assert oct(d.stat().st_mode & 0o777) == "0o700"


def test_init_records_a_server_with_its_anchor(store, tmp_path, monkeypatch):
    from wgt.ctl import newserver
    monkeypatch.setattr(remote, "LOCAL", str(tmp_path / "local"))
    monkeypatch.setattr(remote, "LIST", str(tmp_path / "local" / "servers"))
    newserver.record("lando", "root@1.2.3.4")
    assert (store / "servers" / "lando" / "carriers").is_dir()


def test_an_empty_anchor_goes_and_one_with_jump_files_stays(store):
    tunnels.anchor("gone")
    (store / "servers" / "gone" / "carriers" / ".DS_Store").write_text("")
    assert tunnels.drop_anchor("gone") == "" and not (store / "servers" / "gone").exists()
    tunnels.anchor("kept")
    (store / "servers" / "kept" / "carriers" / "paris.conf").write_text("x")
    assert tunnels.drop_anchor("kept").endswith("kept")
    assert (store / "servers" / "kept" / "carriers" / "paris.conf").exists()
    assert tunnels.drop_anchor("never") == ""
