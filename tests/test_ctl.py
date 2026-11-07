"""wgt-ctl's local parts: the local-file comment, the server list, the
details table and the first-word grammar."""

from __future__ import annotations

import io
import shutil
import subprocess

import pytest

from wgt import ctl, ui
from wgt.ctl import fleet_cmds, jumpfiles, newserver, rebuild, remote
from wgt.errors import WgtError


def test_local_file_comment_replaces_old_one():
    text = "# local file: old.conf\n[Interface]\nPrivateKey = K\n"
    out = jumpfiles.with_local_file_comment(text, "paris.france.conf")
    assert out == "# local file: paris.france.conf\n[Interface]\nPrivateKey = K\n"
    assert jumpfiles.with_local_file_comment(out, "paris.france.conf") == out


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, "LOCAL", str(tmp_path))
    monkeypatch.setattr(remote, "LIST", str(tmp_path / "servers"))
    monkeypatch.setattr(remote, "LINKS", str(tmp_path / "details.tsv"))
    monkeypatch.setattr(remote, "DEVICES", str(tmp_path / "devices"))
    (tmp_path / "servers").write_text("# comment\ndengar  root@1.2.3.4\nboba root@5.6.7.8 /srv/wgt\n")
    (tmp_path / "details.tsv").write_text("boba\tparis.france.conf\tpar-fr\tParis, France\n")
    return tmp_path


def test_servers_and_find(local):
    assert [(s.name, s.target, s.dir) for s in remote.servers()] == [
        ("dengar", "root@1.2.3.4", remote.DEF_DIR), ("boba", "root@5.6.7.8", "/srv/wgt")]
    assert remote.find("2").name == "boba"
    with pytest.raises(WgtError):
        remote.find("nope")


def test_link_of(local):
    assert jumpfiles.link_of("boba", "paris.france.conf") == ("par-fr", "Paris, France")
    assert jumpfiles.link_of("dengar", "paris.france.conf") is None


def test_reserved_names():
    assert not remote.valid_server("all") and not remote.valid_server("init") and remote.valid_server("boba")


def test_all_refuses_single_server_commands(local):
    with pytest.raises(WgtError, match="takes one server"):
        ctl.on("all", "ssh", [])


@pytest.mark.parametrize("line", ["onlyname", "2bad root@h", "ok -oProxyCommand=x", "ok root@h relative/dir",
                                  "all root@h"])
def test_bad_server_lines_refused(local, line):
    (local / "servers").write_text(line + "\n")
    with pytest.raises(WgtError, match="line 1"):
        remote.servers()


def test_dispatch(local, monkeypatch):
    seen = []
    monkeypatch.setattr(remote, "do_wgt", lambda s, args: seen.append((s.name, args)) or 0)
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
    monkeypatch.setattr(remote, "do_wgt", fake)
    assert ctl.on("all", "test", []) == 1


def test_all_log_does_not_follow(local, monkeypatch):
    seen = []
    monkeypatch.setattr(remote, "do_wgt", lambda s, args: seen.append(args) or 0)
    ctl.on("all", "log", [])
    assert all("--no-follow" in a for a in seen)



def test_remove_tears_down_then_forgets(local, monkeypatch):
    sent = []
    monkeypatch.setattr(remote, "ssh", lambda target, cmd, **kw: (sent.append((target, cmd)), (0, ""))[1])
    assert rebuild.do_remove(remote.find("boba"), ["--yes"]) == 0
    target, cmd = sent[0]
    assert target == "root@5.6.7.8"
    assert cmd.index("rollback --purge --yes") < cmd.index("rm -rf /srv/wgt")   # rollback first
    assert "set -e" in cmd                                                       # a failed rollback deletes nothing
    assert [s.name for s in remote.servers()] == ["dengar"]


def test_remove_failure_keeps_the_server_listed(local, monkeypatch):
    monkeypatch.setattr(remote, "ssh", lambda target, cmd, **kw: (1, ""))
    with pytest.raises(WgtError, match="stays in"):
        rebuild.do_remove(remote.find("boba"), ["--yes"])
    assert [s.name for s in remote.servers()] == ["dengar", "boba"]


def test_remove_refuses_a_checkout_near_root(local):
    with pytest.raises(WgtError, match="too close to /"):
        rebuild.do_remove(remote.Server("x", "root@9.9.9.9", "/opt"), ["--yes"])


def test_client_name_skips_flag_values():
    assert fleet_cmds.client_name(["add", "--warp", "off", "par-fr.mac"]) == "par-fr.mac"
    assert fleet_cmds.client_name(["add", "mac", "--warp", "off"]) == "mac"
    assert fleet_cmds.client_name(["add", "--allowed-ips", "10.0.0.0/8", "--yes"]) == ""


def test_parse_move():
    opts, setup, sure = rebuild.parse_move(["--to", "9.9.9.9", "--yes", "--", "--endpoint", "vpn.example.com"])
    assert opts["--to"] == "9.9.9.9" and sure and setup == ["--endpoint", "vpn.example.com"]
    assert rebuild.parse_move([]) == ({"--to": "", "--user": "", "--branch": "main"}, [], False)
    with pytest.raises(WgtError):
        rebuild.parse_move(["--bogus"])


def test_set_target_keeps_checkout(local):
    remote.set_target("boba", "root@9.9.9.9")
    assert [(s.name, s.target, s.dir) for s in remote.servers()] == [
        ("dengar", "root@1.2.3.4", remote.DEF_DIR), ("boba", "root@9.9.9.9", "/srv/wgt")]


@pytest.fixture
def mover(local, monkeypatch):
    """do_move with nothing remote: ssh/scp recorded; <fail> names a step whose ssh fails."""
    calls = []
    state = {"fail": ""}

    def fake_ssh(target, command, **kw):
        calls.append((target, command))
        return (1 if state["fail"] and state["fail"] in command else 0), ""
    monkeypatch.setattr(remote, "ssh", fake_ssh)
    monkeypatch.setattr(remote, "scp", lambda src, dest: calls.append(("scp", dest)) or True)
    monkeypatch.setattr(rebuild, "fetch_backup", lambda s: "/tmp/wireguard-tools.dengar.x.tar.gz")
    monkeypatch.setattr(newserver, "login", lambda t: None)
    monkeypatch.setattr(newserver, "probe", lambda t, a: {})
    monkeypatch.setattr(newserver, "install_checkout", lambda t, b: calls.append((t, "install")))
    monkeypatch.setattr(rebuild, "teardown", lambda t, d: calls.append((t, "teardown")) or 0)
    monkeypatch.setattr(remote, "repo_url", lambda: "https://example.invalid/wgt")
    monkeypatch.setattr(ui, "have_tty", lambda: False)
    return calls, state


def test_move_to_new_machine(mover):
    calls, _ = mover
    s = remote.find("dengar")
    assert rebuild.do_move(s, ["--to", "9.9.9.9", "--yes", "--", "--endpoint", "vpn.example.com"]) == 0
    steps = [(t, c.split(";")[0]) for t, c in calls]
    assert steps.index(("root@9.9.9.9", "install")) < steps.index(("root@1.2.3.4", f"{s.wgt} rollback --yes"))
    restore = next(c for t, c in calls if "restore" in c)
    assert "--endpoint vpn.example.com" in restore
    assert calls[-1] == ("root@1.2.3.4", "teardown")
    assert remote.find("dengar").target == "root@9.9.9.9"


def test_move_failed_restore_keeps_list(mover):
    calls, state = mover
    state["fail"] = "restore"
    assert rebuild.do_move(remote.find("dengar"), ["--to", "9.9.9.9", "--yes"]) == 1
    assert remote.find("dengar").target == "root@1.2.3.4"        # still the old machine
    assert not any(c == "teardown" for _, c in calls)


def test_move_in_place_purges_and_reclones(mover):
    calls, _ = mover
    s = remote.find("dengar")
    assert rebuild.do_move(s, ["--yes"]) == 0
    cmds = [c for _, c in calls]
    assert f"{s.wgt} rollback --purge --yes" in cmds and "install" in cmds
    assert not any(c == "teardown" for c in cmds)


def test_move_needs_default_checkout(mover):
    with pytest.raises(WgtError):
        rebuild.do_move(remote.find("boba"), ["--yes"])


# --- static device numbers across the fleet ---

def answers_from(monkeypatch, held, calls=None):
    """The servers answer `device list --tsv` from <held> (server -> device ->
    number); a server not in it doesn't answer. `device set/rename` go to <calls>."""
    def fake_ssh(target, cmd, **kw):
        name = {"root@1.2.3.4": "dengar", "root@5.6.7.8": "boba"}[target]
        if name not in held:
            return 1, ""
        return 0, "".join(f"{d}\t{'' if n is None else n}\n" for d, n in held[name].items())
    monkeypatch.setattr(remote, "ssh", fake_ssh)
    seen = [] if calls is None else calls
    monkeypatch.setattr(remote, "do_wgt", lambda s, args: seen.append((s.name, args)) or 0)
    return seen


def test_clients_table_has_server_device_config(local, monkeypatch, capsys):
    row = "warp.par-fr.mbp-1\tpar-fr\t10.0.0.5\tnever\t0/0\t0.0.0.0/0\tjump:Paris\t10.9.8.4\n"
    monkeypatch.setattr(remote, "ssh", lambda target, cmd, **kw: (0, row if target == "root@1.2.3.4" else ""))
    fleet_cmds.cmd_clients()
    lines = capsys.readouterr().out.splitlines()
    head = next(line for line in lines if "SERVER" in line)
    assert head.split()[:3] == ["SERVER", "DEVICE", "CONFIG"]
    assert next(line for line in lines if "dengar" in line).split()[:3] == ["dengar", "mbp-1", "warp.par-fr.mbp-1"]


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


def test_add_numbers_a_device_even_if_the_sweep_missed_the_server(local, monkeypatch):
    """A blip while reading must not leave it to take a number outside the fleet's."""
    calls = answers_from(monkeypatch, {"dengar": {}})
    ctl.on("boba", "client", ["add", "phone", "--yes"])
    assert calls[0][1][-2:] == ["--device-number", "2"]


def test_add_all_gives_each_server_the_same_number(local, monkeypatch):
    calls = answers_from(monkeypatch, {"dengar": {}})
    assert ctl.on("all", "client", ["add", "phone", "--yes"]) == 0
    assert [c[1] for c in calls] == [["client", "add", "phone", "--yes", "--device-number", "2"]] * 2


def test_a_failed_add_gives_its_new_number_back(local, monkeypatch):
    answers_from(monkeypatch, {"dengar": {}, "boba": {}})
    monkeypatch.setattr(remote, "do_wgt", lambda s, words: 1)
    assert ctl.on("boba", "client", ["add", "phone", "--yes"]) == 1
    assert "phone" not in fleet_cmds.read_entries()
    assert ctl.on("all", "client", ["add", "phone", "--yes"]) == 1
    assert "phone" not in fleet_cmds.read_entries()


def test_a_failed_add_keeps_a_number_already_in_use(local, monkeypatch):
    (local / "devices").write_text("phone 7\n")
    answers_from(monkeypatch, {"dengar": {}, "boba": {}})
    monkeypatch.setattr(remote, "do_wgt", lambda s, words: 1)
    ctl.on("boba", "client", ["add", "phone", "--yes"])
    assert fleet_cmds.read_entries()["phone"].number == 7


@pytest.mark.parametrize("cmd", ["client", "device"])
def test_all_rename_is_the_whole_device_either_way(local, monkeypatch, cmd):
    calls = answers_from(monkeypatch, {"dengar": {"phone": 5}, "boba": {"phone": 5}})
    (local / "devices").write_text("phone 5\n")
    assert ctl.on("all", cmd, ["rename", "phone", "pixel", "--yes"]) == 0
    assert [c[1] for c in calls] == [["device", "rename", "phone", "pixel", "--yes"]] * 2
    assert fleet_cmds.read_entries()["pixel"].number == 5


def test_all_rename_refuses_to_merge_two_devices(local, monkeypatch):
    calls = answers_from(monkeypatch, {"dengar": {"phone": 5}, "boba": {"pixel": 6}})
    with pytest.raises(WgtError, match="another device"):
        ctl.on("all", "device", ["rename", "phone", "pixel"])
    assert not calls


def test_all_rename_dry_run_keeps_the_local_record(local, monkeypatch):
    answers_from(monkeypatch, {"dengar": {"phone": 5}, "boba": {"phone": 5}})
    (local / "devices").write_text("phone 5\n")
    ctl.on("all", "device", ["rename", "phone", "pixel", "--dry-run"])
    assert "phone" in fleet_cmds.read_entries()


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


# --- first contact, in-place moves, init again ---

def test_first_login_gives_ssh_a_valid_command_line(monkeypatch):
    """Tests fake ssh everywhere else; this is the real argv, and real ssh parses it."""
    runs = []
    monkeypatch.setattr(remote, "can_login", lambda target: False)
    monkeypatch.setattr(ui, "yes", lambda *a, **k: False)
    monkeypatch.setattr(subprocess, "run",
                        lambda args, **k: runs.append(args) or subprocess.CompletedProcess(args, 0))
    newserver.login("root@192.0.2.1")
    argv = runs[0]
    assert "StrictHostKeyChecking=ask" in argv and "StrictHostKeyChecking=yes" not in argv
    monkeypatch.undo()
    if shutil.which("ssh"):
        options = argv[1:-2]   # without ssh itself, the target and the command
        assert subprocess.run(["ssh", "-G", *options, "x.invalid"], capture_output=True).returncode == 0


def test_move_in_place_checks_the_branch_before_purging(mover):
    calls, state = mover
    state["fail"] = "ls-remote"
    with pytest.raises(WgtError, match="nothing has changed"):
        rebuild.do_move(remote.find("dengar"), ["--yes", "--branch", "typo"])
    assert not any("rollback" in c or "install" in c for _, c in calls)


def test_move_in_place_clone_failure_says_how_to_recover(mover, monkeypatch):
    def no_clone(target, branch):
        raise WgtError("clone failed")
    told = io.StringIO()
    monkeypatch.setattr(remote.out, "stream", told)
    monkeypatch.setattr(remote.out, "err", told)
    monkeypatch.setattr(newserver, "install_checkout", no_clone)
    assert rebuild.do_move(remote.find("dengar"), ["--yes"]) == 1
    assert f"{remote.DEF_DIR}/bin/wgt restore" in told.getvalue()


def test_move_failed_rollback_in_place_says_how_to_recover(mover, monkeypatch):
    _, state = mover
    state["fail"] = "rollback"
    told = io.StringIO()
    monkeypatch.setattr(remote.out, "stream", told)
    monkeypatch.setattr(remote.out, "err", told)
    assert rebuild.do_move(remote.find("dengar"), ["--yes"]) == 1
    assert f"{remote.DEF_DIR}/bin/wgt restore" in told.getvalue()


def test_move_enter_keeps_the_old_machine(mover, monkeypatch):
    calls, _ = mover
    monkeypatch.setattr(ui, "have_tty", lambda: True)
    monkeypatch.setattr(ui, "ask", lambda prompt: "dengar" if "Type" in prompt else "")
    assert rebuild.do_move(remote.find("dengar"), ["--to", "9.9.9.9"]) == 0
    assert not any(c == "teardown" for _, c in calls)


@pytest.fixture
def initing(local, monkeypatch):
    """cmd_init with nothing remote; <probe> is what the server says, <setup_rc> how setup ends."""
    state = {"probe": {}, "setup_rc": 0, "installed": False, "setups": 0}

    def fake_ssh(target, command, **kw):
        state["setups"] += 1
        return state["setup_rc"], ""

    monkeypatch.setattr(newserver, "login", lambda target: None)
    monkeypatch.setattr(newserver, "probe", lambda target, address: state["probe"])
    monkeypatch.setattr(newserver, "install_checkout", lambda target, branch: state.update(installed=True))
    monkeypatch.setattr(newserver, "fleet_subnets", list)
    monkeypatch.setattr(remote, "ssh", fake_ssh)
    return state


INIT = ["--address", "9.9.9.9", "--name", "newbie"]


def test_init_adopts_a_finished_server(initing):
    initing["probe"] = {"finished": "1", "saved": "old", "checkout": "1"}
    newserver.cmd_init(INIT)
    assert remote.find("old").target == "root@9.9.9.9"
    assert not initing["installed"] and not initing["setups"]


@pytest.mark.parametrize("probe", [{"saved": "newbie", "checkout": "1"}, {"checkout": "1"}])
def test_init_finishes_an_unfinished_setup(initing, probe):
    initing["probe"] = probe
    newserver.cmd_init(INIT)
    assert initing["installed"] and initing["setups"] == 1
    assert remote.find("newbie").target == "root@9.9.9.9"


def test_init_failed_setup_stays_off_the_list(initing):
    initing["setup_rc"] = 1
    with pytest.raises(WgtError, match="re-run"):
        newserver.cmd_init(INIT)
    assert all(s.name != "newbie" for s in remote.servers())


def test_ssh_passes_a_command_as_the_user_wrote_it(local, monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "run",
                        lambda args, **k: seen.append(args) or subprocess.CompletedProcess(args, 0))
    ctl.do_ssh(remote.find("dengar"), ["ls -la | head"])
    assert seen[0][-1] == "ls -la | head"


@pytest.mark.parametrize("name", ["status", "pull", "move", "client"])
def test_a_server_cant_be_named_after_a_command(name):
    assert not remote.valid_server(name)


def test_every_wgt_command_can_be_reached_from_ctl():
    from wgt import cli
    assert set(remote.WGT_COMMANDS) == set(cli.COMMANDS) - {"watchdog", "debug"}


def test_the_reserved_command_names_match_the_commands():
    assert set(remote.CTL_COMMANDS) == set(ctl.CTL)


def test_the_scripts_sent_to_servers_are_valid_bash(local, monkeypatch):
    """ssh is faked everywhere, so nothing else would notice a broken one."""
    if not shutil.which("bash"):
        pytest.skip("no bash")
    sent = []
    monkeypatch.setattr(remote, "ssh",
                        lambda target, command, **k: sent.append(command) or (0, "uid=0\nos=debian 12\n"))
    monkeypatch.setattr(remote, "repo_url", lambda: "https://example.invalid/wgt")
    newserver.probe("root@h", "h")
    newserver.install_checkout("root@h", "main")
    rebuild.teardown("root@h", remote.DEF_DIR)
    newserver.require_branch("root@h", "main")
    assert len(sent) == 4
    for script in sent:
        checked = subprocess.run(["bash", "-n", "-c", script], capture_output=True, text=True)
        assert checked.returncode == 0, checked.stderr + script
