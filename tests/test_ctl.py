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
    monkeypatch.setattr(ctl, "DEVICES", str(tmp_path / "devices"))
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



def test_remove_tears_down_then_forgets(local, monkeypatch):
    sent = []
    monkeypatch.setattr(ctl, "ssh", lambda target, cmd, **kw: (sent.append((target, cmd)), (0, ""))[1])
    assert ctl.do_remove(ctl.find("boba"), ["--yes"]) == 0
    target, cmd = sent[0]
    assert target == "root@5.6.7.8"
    assert cmd.index("rollback --purge --yes") < cmd.index("rm -rf /srv/wgt")   # rollback first
    assert "set -e" in cmd                                                       # a failed rollback deletes nothing
    assert [s.name for s in ctl.servers()] == ["dengar"]


def test_remove_failure_keeps_the_server_listed(local, monkeypatch):
    monkeypatch.setattr(ctl, "ssh", lambda target, cmd, **kw: (1, ""))
    with pytest.raises(WgtError, match="stays in"):
        ctl.do_remove(ctl.find("boba"), ["--yes"])
    assert [s.name for s in ctl.servers()] == ["dengar", "boba"]


def test_remove_refuses_a_checkout_near_root(local):
    with pytest.raises(WgtError, match="too close to /"):
        ctl.do_remove(ctl.Server("x", "root@9.9.9.9", "/opt"), ["--yes"])


def test_client_name_skips_flag_values():
    from wgt import ctl
    assert ctl.client_name(["add", "--warp", "off", "par-fr.mac"]) == "par-fr.mac"
    assert ctl.client_name(["add", "mac", "--warp", "off"]) == "mac"
    assert ctl.client_name(["add", "--allowed-ips", "10.0.0.0/8", "--yes"]) == ""


def test_parse_move():
    opts, setup, sure = ctl._parse_move(["--to", "9.9.9.9", "--yes", "--", "--endpoint", "vpn.example.com"])
    assert opts["--to"] == "9.9.9.9" and sure and setup == ["--endpoint", "vpn.example.com"]
    assert ctl._parse_move([]) == ({"--to": "", "--user": "", "--branch": "main"}, [], False)
    with pytest.raises(WgtError):
        ctl._parse_move(["--bogus"])


def test_set_target_keeps_checkout(local):
    ctl.set_target("boba", "root@9.9.9.9")
    assert [(s.name, s.target, s.dir) for s in ctl.servers()] == [
        ("dengar", "root@1.2.3.4", ctl.DEF_DIR), ("boba", "root@9.9.9.9", "/srv/wgt")]


@pytest.fixture
def mover(local, monkeypatch):
    """do_move with nothing remote: ssh/scp recorded; <fail> names a step whose ssh fails."""
    calls = []
    state = {"fail": ""}

    def fake_ssh(target, command, **kw):
        calls.append((target, command))
        return (1 if state["fail"] and state["fail"] in command else 0), ""
    monkeypatch.setattr(ctl, "ssh", fake_ssh)
    monkeypatch.setattr(ctl, "scp", lambda src, dest: calls.append(("scp", dest)) or True)
    monkeypatch.setattr(ctl, "fetch_backup", lambda s: "/tmp/wireguard-tools.dengar.x.tar.gz")
    monkeypatch.setattr(ctl, "_login", lambda t: None)
    monkeypatch.setattr(ctl, "_probe", lambda t, a: {})
    monkeypatch.setattr(ctl, "_install", lambda t, b: calls.append((t, "install")))
    monkeypatch.setattr(ctl, "teardown", lambda t, d: calls.append((t, "teardown")) or 0)
    monkeypatch.setattr(ctl.ui, "have_tty", lambda: False)
    return calls, state


def test_move_to_new_machine(mover):
    calls, _ = mover
    s = ctl.find("dengar")
    assert ctl.do_move(s, ["--to", "9.9.9.9", "--yes", "--", "--endpoint", "vpn.example.com"]) == 0
    steps = [(t, c.split(";")[0]) for t, c in calls]
    assert steps.index(("root@9.9.9.9", "install")) < steps.index(("root@1.2.3.4", f"{s.wgt} rollback --yes"))
    restore = next(c for t, c in calls if "restore" in c)
    assert "--endpoint vpn.example.com" in restore
    assert calls[-1] == ("root@1.2.3.4", "teardown")
    assert ctl.find("dengar").target == "root@9.9.9.9"


def test_move_failed_restore_keeps_list(mover):
    calls, state = mover
    state["fail"] = "restore"
    assert ctl.do_move(ctl.find("dengar"), ["--to", "9.9.9.9", "--yes"]) == 1
    assert ctl.find("dengar").target == "root@1.2.3.4"        # still the old machine
    assert not any(c == "teardown" for _, c in calls)


def test_move_in_place_purges_and_reclones(mover):
    calls, _ = mover
    s = ctl.find("dengar")
    assert ctl.do_move(s, ["--yes"]) == 0
    cmds = [c for _, c in calls]
    assert f"{s.wgt} rollback --purge --yes" in cmds and "install" in cmds
    assert not any(c == "teardown" for c in cmds)


def test_move_needs_default_checkout(mover):
    with pytest.raises(WgtError):
        ctl.do_move(ctl.find("boba"), ["--yes"])


# --- static device numbers across the fleet ---

def answers_from(monkeypatch, held, calls=None):
    """The servers answer `device list --tsv` from <held> (server -> device ->
    number); a server not in it doesn't answer. `device set/rename` go to <calls>."""
    def fake_ssh(target, cmd, **kw):
        name = {"root@1.2.3.4": "dengar", "root@5.6.7.8": "boba"}[target]
        if name not in held:
            return 1, ""
        return 0, "".join(f"{d}\t{'' if n is None else n}\n" for d, n in held[name].items())
    monkeypatch.setattr(ctl, "ssh", fake_ssh)
    seen = [] if calls is None else calls
    monkeypatch.setattr(ctl, "do_wgt", lambda s, args: seen.append((s.name, args)) or 0)
    return seen


def test_add_takes_the_next_low_number_and_remembers_it(local, monkeypatch):
    calls = answers_from(monkeypatch, {"dengar": {"mac": 2}, "boba": {"mac": 2}})
    assert ctl.on("boba", "client", ["add", "phone", "--yes"]) == 0
    assert calls == [("boba", ["client", "add", "phone", "--yes", "--device-number", "3"])]
    assert (local / "devices").read_text().splitlines()[1:] == ["mac 2", "phone 3"]


def test_add_reuses_the_number_the_fleet_has_for_the_device(local, monkeypatch):
    calls = answers_from(monkeypatch, {"dengar": {"mac": 2, "phone": 5}, "boba": {"mac": 2}})
    ctl.on("boba", "client", ["add", "warp.phone", "--yes"])
    assert calls[0][1][-2:] == ["--device-number", "5"]


def test_add_uses_local_devices_for_a_server_that_is_down(local, monkeypatch):
    (local / "devices").write_text("phone 9\n")
    calls = answers_from(monkeypatch, {"boba": {}})
    ctl.on("boba", "client", ["add", "phone", "--yes"])
    assert calls[0][1][-2:] == ["--device-number", "9"]
    assert "removed" not in (local / "devices").read_text()   # dengar might still have it


def test_add_leaves_the_number_off_a_server_that_cannot_take_it(local, monkeypatch):
    calls = answers_from(monkeypatch, {"dengar": {}})          # boba: not updated yet
    ctl.on("boba", "client", ["add", "phone", "--yes"])
    assert "--device-number" not in calls[0][1]


def test_add_all_numbers_each_server_that_answers(local, monkeypatch):
    calls = answers_from(monkeypatch, {"dengar": {}})
    assert ctl.on("all", "client", ["add", "phone", "--yes"]) == 0
    assert calls == [("dengar", ["client", "add", "phone", "--yes", "--device-number", "2"]),
                     ("boba", ["client", "add", "phone", "--yes"])]


def test_add_keeps_a_number_given_by_hand(local, monkeypatch):
    calls = answers_from(monkeypatch, {"dengar": {}, "boba": {}})
    ctl.on("boba", "client", ["add", "phone", "--device-number", "40"])
    assert calls[0][1] == ["client", "add", "phone", "--device-number", "40"]


def test_a_removed_device_is_flagged_and_its_number_not_reused(local, monkeypatch):
    (local / "devices").write_text("old 7\n")
    calls = answers_from(monkeypatch, {"dengar": {}, "boba": {}})
    ctl.on("boba", "client", ["add", "phone", "--yes"])
    assert calls[0][1][-1] == "8"
    assert "old 7 removed " in (local / "devices").read_text()


def test_devices_sync_gives_one_number_on_every_server(local, monkeypatch):
    calls = answers_from(monkeypatch, {"dengar": {"mac": None}, "boba": {"mac": None}})
    assert ctl.main(["devices", "sync"]) == 0
    assert calls == [("dengar", ["device", "set", "mac", "2"]), ("boba", ["device", "set", "mac", "2"])]


def test_devices_fix_sets_every_holder_and_refuses_a_taken_number(local, monkeypatch):
    calls = answers_from(monkeypatch, {"dengar": {"mac": 2, "tv": 3}, "boba": {"mac": 200}})
    assert ctl.main(["devices", "fix", "mac", "3"]) == 1    # tv's
    assert calls == []
    assert ctl.main(["devices", "fix", "mac", "9"]) == 0
    assert calls == [("dengar", ["device", "set", "mac", "9"]), ("boba", ["device", "set", "mac", "9"])]
    assert "mac 9" in (local / "devices").read_text().splitlines()


def test_devices_forget_refuses_while_a_server_might_have_it(local, monkeypatch):
    (local / "devices").write_text("old 7 removed 2026-01-01\n")
    answers_from(monkeypatch, {"boba": {}})
    assert ctl.main(["devices", "forget", "old"]) == 1      # dengar didn't answer
    answers_from(monkeypatch, {"dengar": {"old": 7}, "boba": {}})
    assert ctl.main(["devices", "forget", "old"]) == 1      # still on dengar
    answers_from(monkeypatch, {"dengar": {}, "boba": {}})
    assert ctl.main(["devices", "forget", "old"]) == 0
    assert "old" not in (local / "devices").read_text().replace("# wgt-ctl", "")


def test_rename_everywhere(local, monkeypatch):
    (local / "devices").write_text("phone 5\n")
    calls = answers_from(monkeypatch, {"dengar": {"phone": 5}, "boba": {"mac": 2}})
    assert ctl.on("all", "client", ["rename", "phone", "pixel"]) == 0
    assert calls == [("dengar", ["device", "rename", "phone", "pixel"])]   # only where it is
    assert (local / "devices").read_text().splitlines()[1:] == ["pixel 5"]


def test_rename_everywhere_refuses_config_names_clashes_and_a_missing_server(local, monkeypatch):
    calls = answers_from(monkeypatch, {"dengar": {"phone": 5, "pixel": 6}, "boba": {}})
    with pytest.raises(WgtError, match="device names"):
        ctl.on("all", "client", ["rename", "warp.phone", "warp.pixel"])
    with pytest.raises(WgtError, match="already exists"):
        ctl.on("all", "client", ["rename", "phone", "pixel"])
    answers_from(monkeypatch, {"boba": {"phone": 5}}, calls)
    with pytest.raises(WgtError, match="nothing renamed"):
        ctl.on("all", "client", ["rename", "phone", "pixel"])
    assert calls == []


def test_rename_everywhere_force_can_be_finished_later(local, monkeypatch):
    (local / "devices").write_text("phone 5\n")
    calls = answers_from(monkeypatch, {"boba": {"phone": 5}})
    assert ctl.on("all", "client", ["rename", "phone", "pixel", "--force"]) == 0
    calls.clear()
    answers_from(monkeypatch, {"dengar": {"phone": 5}, "boba": {"pixel": 5}}, calls)
    assert ctl.on("all", "client", ["rename", "phone", "pixel"]) == 0
    assert calls == [("dengar", ["device", "rename", "phone", "pixel"])]
    assert (local / "devices").read_text().splitlines()[1:] == ["pixel 5"]
