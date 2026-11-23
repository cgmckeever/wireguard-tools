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


@pytest.mark.parametrize("renamed", ["z.iphone", "0.iphone"])   # sorting after or before the new one
def test_a_live_key_wins_over_a_header_whatever_the_order(store, renamed):
    # a.iphone renamed (its key kept) and a new a.iphone added: the old copy is the renamed one's
    path = put(store, "iphone", "dengar.a.iphone.conf", conf("a.iphone", "K1"))
    actions = run(store, {renamed: conf(renamed, "K1"), "a.iphone": conf("a.iphone", "K2")})
    assert ("update", "dengar.a.iphone.conf", renamed) in kinds(actions)
    assert ("refuse", "dengar.a.iphone.conf", "a.iphone") in kinds(actions)
    assert path.read_text() == conf(renamed, "K1")


def test_a_name_freed_by_a_retire_takes_the_new_copy_in_the_same_sync(store):
    # the server dropped Old.iphone and has old.iphone: one file on a Mac, so the old copy
    # retires first and the new one arrives in this sync, not the next
    old = put(store, "iphone", "dengar.Old.iphone.conf", conf("Old.iphone", "K1"))
    actions = tunnels.plan("dengar", {"old.iphone": conf("old.iphone", "K2")},
                           tunnels.local_files(tunnels.all_devices()), True)
    tunnels.apply(tunnels.refuse_taken(actions))
    assert kinds(actions) == [("new", "dengar.old.iphone.conf", "old.iphone"),
                              ("retire", "dengar.Old.iphone.conf", "Old.iphone")]
    assert (old.parent / "dengar.old.iphone.conf").read_text() == conf("old.iphone", "K2")
    assert any(f.startswith("dengar.Old.iphone.conf.") and f.endswith(".bak") for f in os.listdir(old.parent))


def test_a_hand_copied_config_leaves_the_real_copy_updated_and_is_warned_about(store, monkeypatch):
    # ipad's folder sorts first, but the copy at the config's own name is the one updated
    real = put(store, "iphone", "dengar.iphone.conf", conf("iphone", "K1"))
    mine = put(store, "ipad", "borrowed.conf", conf("iphone", "K1"))
    told = io.StringIO()
    monkeypatch.setattr(remote.out, "err", told)
    new = conf("iphone", "K1", body="MTU = 1280\n")
    actions = run(store, {"iphone": new})
    tunnels.report("dengar", actions)
    assert kinds(actions) == [("duplicate", "borrowed.conf", "iphone"), ("update", "dengar.iphone.conf", "iphone")]
    assert real.read_text() == new and mine.read_text() == conf("iphone", "K1")
    assert "borrowed.conf holds its key too" in told.getvalue()


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


def test_a_folder_two_device_names_reach_is_read_once(store):
    # On a Mac devices/iPhone is devices/iphone: a server's "iphone" next to the folder
    # "iPhone" listed its files twice, and the second retire of one failed (it was gone).
    gone = put(store, "phone", "dengar.old.phone.conf", conf("old.phone", "GONE"))
    os.symlink("phone", store / "devices" / "phone2")   # one folder, two names, on any system
    assert list(tunnels.local_files(["phone", "phone2"])) == [str(gone)]
    actions = tunnels.plan("dengar", {}, tunnels.local_files(["phone", "phone2"]), True)
    tunnels.apply(actions)
    assert kinds(actions) == [("retire", "dengar.old.phone.conf", "old.phone")]


def test_two_devices_differing_only_in_case_are_told_apart_in_the_refusal(store):
    actions = tunnels.plan("dengar", {"Phone": conf("Phone", "K1"), "phone": conf("phone", "K2")}, {}, True)
    assert kinds(actions) == [("new", "dengar.Phone.conf", "Phone"), ("refuse", "dengar.phone.conf", "phone")]
    assert "another of this server's configs" in actions[1].note


def test_nothing_retires_unless_the_list_is_complete(store):
    put(store, "iphone", "dengar.old.iphone.conf", conf("old.iphone", "GONE"))
    assert run(store, {}, complete=False) == []


def test_bak_files_are_not_copies(store):
    put(store, "iphone", "x.conf.20261112-120000.bak", conf("warp.iphone", "K1"))
    assert kinds(run(store, {"warp.iphone": conf("warp.iphone", "K1")})) == [
        ("new", "dengar.warp.iphone.conf", "warp.iphone")]


SWEPT = "[Peer]\nAllowedIPs = 162.159.192.1/32, 10.9.8.0/24\n"   # a warped carrier: WARP only
PLAIN = "DNS = 1.1.1.1\n[Peer]\nAllowedIPs = 0.0.0.0/0, ::/0\n"


def test_a_warped_carrier_next_to_the_plain_copy_is_untouched(store):
    plain = put(store, "mbp-1", "dengar.chi-us.mbp-1.conf", conf("chi-us.mbp-1", "K1", body=PLAIN))
    warped = put(store, "mbp-1", "warped.dengar.chi-us.mbp-1.conf", conf("chi-us.mbp-1", "K1", body=SWEPT))
    new = conf("chi-us.mbp-1", "K1", body="MTU = 1280\n" + PLAIN)
    assert kinds(run(store, {"chi-us.mbp-1": new})) == [
        ("update", "dengar.chi-us.mbp-1.conf", "chi-us.mbp-1")]
    assert plain.read_text() == new
    assert warped.read_text() == conf("chi-us.mbp-1", "K1", body=SWEPT)


def test_a_warped_carrier_alone_gets_a_plain_copy_beside_it(store):
    warped = put(store, "mbp-1", "warped.dengar.chi-us.mbp-1.conf", conf("chi-us.mbp-1", "K1", body=SWEPT))
    issued = conf("chi-us.mbp-1", "K1", body=PLAIN)
    assert kinds(run(store, {"chi-us.mbp-1": issued})) == [
        ("new", "dengar.chi-us.mbp-1.conf", "chi-us.mbp-1")]
    assert (warped.parent / "dengar.chi-us.mbp-1.conf").read_text() == issued
    assert warped.read_text() == conf("chi-us.mbp-1", "K1", body=SWEPT)


def test_a_warped_carrier_is_untouched_by_a_server_that_sorts_after_it(store):
    put(store, "mbp-1", "zuckuss.chi-us.mbp-1.conf", conf("chi-us.mbp-1", "K1", "zuckuss", PLAIN))
    swept = conf("chi-us.mbp-1", "K1", "zuckuss", SWEPT)
    warped = put(store, "mbp-1", "warped.zuckuss.chi-us.mbp-1.conf", swept)
    issued = {"chi-us.mbp-1": conf("chi-us.mbp-1", "K1", "zuckuss", "MTU = 1280\n" + PLAIN)}
    actions = tunnels.plan("zuckuss", issued, tunnels.local_files(tunnels.all_devices()), True)
    tunnels.apply(actions)
    assert kinds(actions) == [("update", "zuckuss.chi-us.mbp-1.conf", "chi-us.mbp-1")]
    assert warped.read_text() == swept


def test_a_rotate_neither_updates_nor_retires_a_warped_carrier(store):
    # its header still names the config, and its old key is no longer live
    warped = put(store, "mbp-1", "warped.dengar.chi-us.mbp-1.conf", conf("chi-us.mbp-1", "OLD", body=SWEPT))
    assert kinds(run(store, {"chi-us.mbp-1": conf("chi-us.mbp-1", "NEW", body=PLAIN)})) == [
        ("new", "dengar.chi-us.mbp-1.conf", "chi-us.mbp-1")]
    assert warped.read_text() == conf("chi-us.mbp-1", "OLD", body=SWEPT)
    assert sorted(os.listdir(warped.parent)) == ["dengar.chi-us.mbp-1.conf", "warped.dengar.chi-us.mbp-1.conf"]


WARP_ALONE = "[Interface]\nPrivateKey = WARPKEY\nAddress = 172.16.0.2/32\n[Peer]\nEndpoint = 162.159.192.1:2408\n"


def test_the_sweeps_warp_alone_config_is_never_matched_updated_or_retired(store):
    # wireguard-tunnels writes config/warp.<device>.conf (the device's WARP alone, no
    # header): wgt-ctl's add, update, rotate and retire all pass it by
    warp = put(store, "iphone", "warp.iphone.conf", WARP_ALONE)
    old = put(store, "iphone", "dengar.old.iphone.conf", conf("old.iphone", "GONE"))
    actions = run(store, {"warp.iphone": conf("warp.iphone", "K1"), "iphone": conf("iphone", "K2")})
    assert kinds(actions) == [("new", "dengar.iphone.conf", "iphone"),
                              ("new", "dengar.warp.iphone.conf", "warp.iphone"),
                              ("retire", "dengar.old.iphone.conf", "old.iphone")]
    assert not old.exists()
    run(store, {"warp.iphone": conf("warp.iphone", "K3")})
    assert warp.read_text() == WARP_ALONE
    assert "warp.iphone.conf" in os.listdir(warp.parent)


@pytest.mark.parametrize("there", [False, True])
def test_a_server_named_warp_never_writes_the_sweeps_warp_alone_name(store, there):
    # server "warp" + config "iphone" would be warp.iphone.conf: the sweep's file
    if there:
        put(store, "iphone", "warp.iphone.conf", WARP_ALONE)
    actions = tunnels.plan("warp", {"iphone": conf("iphone", "K1", server="warp")},
                           tunnels.local_files(tunnels.all_devices()), True)
    tunnels.apply(actions)
    assert kinds(actions) == [("refuse", "warp.iphone.conf", "iphone")]
    d = store / "devices" / "iphone" / "config"
    assert (d / "warp.iphone.conf").read_text() == WARP_ALONE if there else not d.exists()


def test_a_name_that_differs_only_in_case_is_the_same_file_on_a_mac(store):
    # Warp.iphone.conf would land on the sweep's warp.iphone.conf, and dengar.iphone.conf
    # on a hand-made Dengar.iphone.conf, on a case-insensitive file system
    put(store, "iphone", "warp.iphone.conf", WARP_ALONE)
    mine = put(store, "iphone", "Dengar.iphone.conf", "[Interface]\nPrivateKey = MINE\n")
    actions = tunnels.plan("Warp", {"iphone": conf("iphone", "K1", server="Warp")},
                           tunnels.local_files(tunnels.all_devices()), True)
    assert kinds(actions) == [("refuse", "Warp.iphone.conf", "iphone")]
    actions = run(store, {"iphone": conf("iphone", "K2")})
    assert kinds(actions) == [("refuse", "dengar.iphone.conf", "iphone")]
    assert mine.read_text() == "[Interface]\nPrivateKey = MINE\n"


def test_the_sweeps_name_is_passed_by_in_any_spelling_of_its_folder(store):
    # devices/IPhone reaches the iphone folder (a Mac's spelling, a symlink here): its
    # warp.iphone.conf is still the sweep's
    put(store, "iphone", "warp.iphone.conf", WARP_ALONE)
    if not (store / "devices" / "IPhone").exists():   # a case-sensitive file system
        os.symlink("iphone", store / "devices" / "IPhone")
    assert tunnels.local_files(["IPhone", "iphone"]) == {}


def test_a_folder_reached_another_way_still_refuses_a_hand_made_file(store, monkeypatch):
    # devices/phone -> iphone: local_files lists that folder once, as iphone's
    mine = put(store, "iphone", "dengar.phone.conf", "[Interface]\nPrivateKey = MINE\n")
    os.symlink("iphone", store / "devices" / "phone")
    out = f"SERVER_NAME=dengar\n{tunnels.MARK}dengar.phone.conf\n{conf('phone', 'K1')}"
    monkeypatch.setattr(remote, "ssh", lambda target, command, **k: (0, out))
    assert tunnels.deliver(remote.Server("dengar", "root@h")) == 1
    assert mine.read_text() == "[Interface]\nPrivateKey = MINE\n"


def test_an_update_whose_write_fails_leaves_the_config_in_place(store, monkeypatch):
    path = put(store, "iphone", "dengar.iphone.conf", conf("iphone", "K1"))

    def full(p, text, mode=0o600):
        raise OSError("disk full")
    monkeypatch.setattr(remote, "write_local", full)
    with pytest.raises(OSError):
        run(store, {"iphone": conf("iphone", "K1", body="MTU = 1280\n")})
    assert path.read_text() == conf("iphone", "K1")


def test_two_updates_in_one_second_keep_both_old_copies(store, monkeypatch):
    monkeypatch.setattr(tunnels.time, "strftime", lambda fmt, *a: "20260101-000000")
    path = put(store, "iphone", "dengar.iphone.conf", conf("iphone", "K1"))
    run(store, {"iphone": conf("iphone", "K1", body="MTU = 1280\n")})
    run(store, {"iphone": conf("iphone", "K1", body="MTU = 1300\n")})
    baks = sorted(f for f in os.listdir(path.parent) if f.endswith(".bak"))
    assert len(baks) == 2
    assert {(path.parent / b).read_text() for b in baks} == {conf("iphone", "K1"),
                                                            conf("iphone", "K1", body="MTU = 1280\n")}


def test_the_store_is_wg_tunnels_as_written_and_absolute(monkeypatch):
    # wireguard-tunnels (store.root, tunnel.sh) doesn't expand a "~" in it: neither do we,
    # and a path that isn't absolute is refused, not taken as one under the current folder
    monkeypatch.setenv("WG_TUNNELS", "/srv/elsewhere")
    assert tunnels.root() == "/srv/elsewhere"
    for relative in ("~/elsewhere", "wg-tunnels", "./x"):
        monkeypatch.setenv("WG_TUNNELS", relative)
        with pytest.raises(WgtError, match="absolute"):
            tunnels.root()
    monkeypatch.delenv("WG_TUNNELS")
    assert tunnels.root() == os.path.expanduser("~/wg-tunnels")


def test_a_renamed_devices_copy_stays_put_with_a_note(store):
    put(store, "phone", "dengar.warp.phone.conf", conf("warp.phone", "K1"))
    [a] = run(store, {"warp.iphone": conf("warp.iphone", "K1")})
    assert (a.kind, a.path.split(os.sep)[-3]) == ("update", "phone")
    assert "now 'iphone'" in a.note


def test_fetch_reads_every_config_by_its_header(store, monkeypatch):
    m = tunnels.MARK
    text = f"{m}dengar.warp.iphone.conf\n{conf('warp.iphone', 'K1')}{m}stray.conf\n[Interface]\n"
    monkeypatch.setattr(remote, "ssh", lambda target, command, **k: (0, text))
    assert tunnels.fetch(remote.Server("dengar", "root@h")) == ({"warp.iphone": conf("warp.iphone", "K1")}, "", set())
    monkeypatch.setattr(remote, "ssh", lambda target, command, **k: (0, "SERVER_NAME=dengar2\n" + text))
    assert tunnels.fetch(remote.Server("dengar", "root@h"))[1] == "dengar2"
    # a login script's chatter before it isn't the name
    monkeypatch.setattr(remote, "ssh", lambda target, command, **k: (0, "Welcome!\n" + text))
    assert tunnels.fetch(remote.Server("dengar", "root@h"))[1] == ""
    monkeypatch.setattr(remote, "ssh", lambda target, command, **k: (255, ""))
    with pytest.raises(WgtError):
        tunnels.fetch(remote.Server("dengar", "root@h"))


def test_a_config_without_a_header_keeps_its_copy(store, monkeypatch):
    # The server still has it (just unnamed): its key is live, so its copy isn't retired.
    kept = put(store, "iphone", "dengar.iphone.conf", conf("iphone", "K1"))
    m = tunnels.MARK
    headerless = conf("iphone", "K1").split("\n", 1)[1]
    monkeypatch.setattr(remote, "ssh", lambda target, command, **k: (0, f"{m}dengar.iphone.conf\n{headerless}"))
    monkeypatch.setattr(remote, "servers", lambda: [remote.Server("dengar", "root@h")])
    issued, _, keys = tunnels.fetch(remote.Server("dengar", "root@h"))
    assert issued == {} and keys == {"K1"}
    tunnels.deliver(remote.Server("dengar", "root@h"))
    assert kept.read_text() == conf("iphone", "K1")
    assert not [f for f in os.listdir(kept.parent) if f.endswith(".bak")]


def test_the_delivered_copy_wins_over_a_hand_copy_in_another_folder(store):
    # A hand copy in a folder that sorts first: after a rotate (header match) or a
    # rename (key match), the copy delivery named is the one kept up to date.
    hand = put(store, "aaa", "my-copy.conf", conf("phone", "K1"))
    own = put(store, "phone", "dengar.phone.conf", conf("phone", "K1"))
    rotated = run(store, {"phone": conf("phone", "K2")})
    assert ("update", "dengar.phone.conf", "phone") in kinds(rotated)
    assert own.read_text() == conf("phone", "K2")
    assert ("retire", "my-copy.conf", "phone") in kinds(rotated) and not hand.exists()
    hand = put(store, "aaa", "my-copy.conf", conf("phone", "K2"))
    renamed = run(store, {"tablet": conf("tablet", "K2")})
    assert ("update", "dengar.phone.conf", "tablet") in kinds(renamed)
    assert ("duplicate", "my-copy.conf", "tablet") in kinds(renamed)
    assert own.read_text() == conf("tablet", "K2") and hand.read_text() == conf("phone", "K2")


def test_flags_before_the_subcommand_are_read_as_wgt_reads_them():
    from wgt.ctl import fleet_cmds
    assert remote.subcommand(["--yes", "add", "phone"]) == ("add", ["--yes", "phone"])
    assert remote.subcommand(["--label", "add", "rename"]) == ("rename", ["--label", "add"])
    assert fleet_cmds.client_name(["--yes", "add", "--warp", "off", "phone"]) == "phone"


def test_a_non_utf8_byte_in_a_config_is_delivered_as_it_was(store, monkeypatch):
    m, seen = tunnels.MARK, {}
    text = conf("iphone", "K1", body="# exit: Z\udce9rich\n")   # a Latin-1 é, as surrogateescape reads it

    def ssh(target, command, **k):
        seen.update(k)
        return 0, f"{m}dengar.iphone.conf\n{text}"
    monkeypatch.setattr(remote, "ssh", ssh)
    assert tunnels.deliver(remote.Server("dengar", "root@h")) == 0
    assert seen["errors"] == "surrogateescape"
    assert b"Z\xe9rich" in (store / "devices" / "iphone" / "config" / "dengar.iphone.conf").read_bytes()


def test_the_name_in_the_configs_is_the_servers_own(store):
    issued = {"a.x": conf("a.x", "K", server="crash")}
    assert tunnels.server_name(issued, remote.Server("boba", "root@h")) == "crash"
    assert tunnels.server_name({}, remote.Server("boba", "root@h")) == "boba"
    assert tunnels.server_name({}, remote.Server("boba", "root@h"), "crash") == "crash"


def test_a_renamed_servers_last_config_still_retires(store, monkeypatch):
    # listed as dengar, SERVER_NAME dengar2: with no configs left, only the state names it
    gone = put(store, "iphone", "dengar2.phone.conf", conf("phone", "K1", server="dengar2"))
    monkeypatch.setattr(remote, "ssh", lambda target, command, **k: (0, "SERVER_NAME=dengar2\n"))
    assert tunnels.deliver(remote.Server("dengar", "root@h")) == 0
    assert not gone.exists()


@pytest.mark.parametrize("cmd,args,delivers", [
    ("client", ["add", "phone"], True), ("client", ["rotate", "warp.phone"], True),
    ("client", ["rename", "a.phone", "b.phone"], True), ("client", ["remove", "a.phone"], True),
    ("device", ["rename", "phone", "iphone"], True), ("client", ["add", "phone", "--dry-run"], False),
    ("client", ["list"], True), ("client", [], True), ("client", ["list", "--tsv"], False),
    ("client", ["disable", "a.phone"], False), ("device", ["set", "phone"], False),
    ("setup", [], True), ("setup", ["--name", "dengar2"], True), ("setup", ["--dry-run"], False),
    ("client", ["--yes", "add", "phone"], True), ("client", ["--yes", "remove", "a.phone"], True),
    ("client", ["--yes", "disable", "a.phone"], False), ("client", ["--yes"], True),
    ("restore", ["/root/x.tar.gz"], True),
])
def test_which_commands_deliver(cmd, args, delivers):
    assert tunnels.changes_configs(cmd, args) is delivers


@pytest.mark.parametrize("args,dry", [
    (["add", "phone", "--dry-run"], True), (["add", "phone", "--label=x", "--dry-run"], True),
    (["add", "phone", "--label", "--dry-run"], False),   # wgt reads it as the label: a real add
    (["add", "phone"], False),
])
def test_a_dry_run_is_read_as_wgt_reads_it(args, dry):
    assert remote.dry_run(args) is dry


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


def test_a_setup_or_restore_that_stopped_delivers_nothing(store, monkeypatch):
    # setup rewrites every config before it asks to restart wg0: after a declined restart
    # they hold settings wg0 isn't running yet (a new --port), so devices mustn't get them
    done = []
    monkeypatch.setattr(tunnels, "deliver", lambda s: done.append(s.name))
    s = remote.Server("dengar", "root@h")
    monkeypatch.setattr(remote, "do_wgt", lambda s, args: 1)
    assert ctl._run(s, "setup", ["--port", "51999"]) == 1 and done == []
    assert ctl._run(s, "restore", ["/root/x.tar.gz"]) == 1 and done == []   # maybe half extracted
    monkeypatch.setattr(remote, "do_wgt", lambda s, args: 0)
    assert ctl._run(s, "setup", ["--port", "51999"]) == 0 and done == ["dengar"]


def test_configs_naming_another_listed_server_are_not_delivered(store, tmp_path, monkeypatch):
    # dengar given boba's name (setup --name boba): boba's copies would retire here
    listed = tmp_path / "servers"
    listed.write_text("dengar root@1.2.3.4\nboba root@5.6.7.8\nalias root@1.2.3.4\n")
    monkeypatch.setattr(remote, "LIST", str(listed))
    theirs = put(store, "iphone", "boba.iphone.conf", conf("iphone", "KB", server="boba"))
    out = f"SERVER_NAME=boba\n{tunnels.MARK}boba.phone.conf\n{conf('phone', 'KD', server='boba')}"
    monkeypatch.setattr(remote, "ssh", lambda target, command, **k: (0, out))
    with pytest.raises(WgtError, match="boba"):
        tunnels.deliver(remote.Server("dengar", "root@1.2.3.4"))
    assert theirs.exists()
    # the same machine listed twice is one server
    out = f"SERVER_NAME=dengar\n{tunnels.MARK}dengar.phone.conf\n{conf('phone', 'KD')}"
    assert tunnels.deliver(remote.Server("alias", "root@1.2.3.4")) == 0


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
