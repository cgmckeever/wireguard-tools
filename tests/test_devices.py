"""Static device addresses: the numbers, the commands, the watchdog's rules, the
fleet's record (wgt/fleet.py; wgt-ctl's use of it is in test_ctl.py)."""

from __future__ import annotations

import time

import pytest

from wgt import checks, clients, devices, fleet, live, ops, render, rules, store, units, watchdog
from wgt.config import resolve
from wgt.const import DEVICE_CHAIN, DEVICE_SOURCE_CHAIN, DEVICE_WATCHDOG, Paths, device_ip
from wgt.errors import WgtError
from wgt.fleet import Entry

NOW = int(time.time())


def numbers(ctx):
    return {d.name: d.number for d in ctx.snap.devices}


@pytest.fixture
def again(make_ctx):
    """The same server, a new command (the last one's lock let go)."""
    def build(ctx, argv=()):
        ops.release_lock(ctx)
        return make_ctx(argv=list(argv))
    return build


# --- devices.db ---

def test_devices_db_round_trip():
    text = store.put_device(store.put_device(None, "mac", 2), "phone", 254)
    assert [(d.name, d.number) for d in store.parse_devices(text)] == [("mac", 2), ("phone", 254)]
    assert [d.name for d in store.parse_devices(store.put_device(text, "mac"))] == ["phone"]


@pytest.mark.parametrize("line", ["mac", "mac x", "mac 1", "mac 255", "a.b 5", "mac 5 6"])
def test_devices_db_bad_lines_refused(line):
    with pytest.raises(WgtError, match="devices.db"):
        store.parse_devices(line + "\n")


def test_direct_numbers_count_down_and_ignore_wgt_ctls():
    assert store.next_direct_number([]) == 254
    assert store.next_direct_number([store.Device("a", 2)]) == 254
    assert store.next_direct_number([store.Device("a", 254), store.Device("b", 253)]) == 252
    assert store.next_direct_number([store.Device("a", 128)]) is None


# --- the subnet can't overlap the static network ---

@pytest.mark.parametrize("subnet", ["10.9.8.0/24", "10.9.0.0/16", "10.9.8.128/25"])
def test_subnet_overlapping_the_static_network_refused(subnet):
    cfg = resolve({"WG_SUBNET": subnet}, {}, {}, paths=Paths(wg_dir="/nonexistent"))
    with pytest.raises(WgtError, match="overlaps 10.9.8.0/24"):
        cfg.finalize(kernel_ipv6=False)


def test_subnet_with_leading_zeros_is_a_plain_refusal():
    cfg = resolve({"WG_SUBNET": "010.1.1.0/24"}, {}, {}, paths=Paths(wg_dir="/nonexistent"))
    with pytest.raises(WgtError, match="not a network address"):
        cfg.finalize(kernel_ipv6=False)


# --- rendering ---

def test_server_holds_the_first_static_address(server):
    text = render.render_wg_conf(server.cfg, server.snap, server.pub_if, store.psk_reader(server.paths))
    assert next(line for line in text.splitlines() if line.startswith("Address = ")).endswith(", 10.9.8.1/24")


def test_device_rules_read_back_exactly_once(server, make_ctx):
    """Their text must be how `iptables -S` prints them, or wgt test fails and
    setup restarts wg0 every time (live.ipt_count compares the text)."""
    ctx = make_ctx()
    wg, ip = server.cfg.WG_IF, server.cfg.server_ip
    printed = [f"-A PREROUTING -d 10.9.8.0/24 -i {wg} -j WGT-DEVICES",
               "-A OUTPUT -d 10.9.8.0/24 -j WGT-DEVICES",
               f"-A POSTROUTING -o {wg} -m conntrack --ctstate DNAT -j WGT-DEVICE-SRC",
               "-A WGT-PEERS -j REJECT --reject-with icmp-port-unreachable",
               f"-A FORWARD -i {wg} -o {wg} -j WGT-PEERS",
               "-A WGT-SERVER -j REJECT --reject-with icmp-port-unreachable",
               "-A WGT-SERVER -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
               f"-A INPUT -d {ip}/32 -i {wg} -j WGT-SERVER",
               f"-A INPUT -d 10.9.8.1/32 -i {wg} -j WGT-SERVER"]
    if server.cfg.ipv6:
        printed += [f"-A FORWARD -i {wg} -o {wg} -j REJECT --reject-with icmp6-port-unreachable",
                    "-A WGT-SERVER -j REJECT --reject-with icmp6-port-unreachable",
                    "-A WGT-SERVER -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
                    f"-A INPUT -d {server.cfg.p6}::1/128 -i {wg} -j WGT-SERVER"]
    found = rules.device_ipt_rules(server.cfg)
    assert len(found) == len(printed)
    for line, (x, t, c, rule) in zip(printed, found):
        ctx.runner.answers[f"{x} -t {t} -S {c}"] = (0, line + "\n")
        assert live.ipt_count(ctx.runner, x, t, c, rule) == 1


def test_chains_refuse_until_the_watchdog_allows(server):
    """Refusal first, as rules go in on top: an empty chain fails closed. The
    server's public IP isn't caught: only its tunnel addresses are."""
    found = rules.device_ipt_rules(server.cfg)
    in_chain = [r for x, _, c, r in found if x == "iptables" and c == "WGT-SERVER"]
    assert in_chain[0].startswith("-j REJECT") and "ESTABLISHED" in in_chain[1]
    assert [r for x, _, c, r in found if x == "iptables" and c == "WGT-PEERS"][0].startswith("-j REJECT")
    inputs = [r for x, _, c, r in found if x == "iptables" and c == "INPUT"]
    assert len(inputs) == 2 and all(f"-i {server.cfg.WG_IF} -j WGT-SERVER" in r for r in inputs)
    assert not any("ctstate DNAT -j ACCEPT" in r for x, t, c, r in found if c == "FORWARD")


def test_wgt_test_checks_the_device_watchdog_timer(make_ctx, capsys, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    checks.Tests(make_ctx()).run()
    captured = capsys.readouterr()
    assert "device watchdog timer enabled and running" in captured.out + captured.err


def test_setup_writes_the_device_watchdog(make_ctx):
    ctx = make_ctx(answers={f"systemctl is-active -q {DEVICE_WATCHDOG}.timer": (3, "")})
    units.write_device_watchdog(ctx)
    timer = ctx.paths.unit_file(f"{DEVICE_WATCHDOG}.timer")
    service = ctx.paths.unit_file(f"{DEVICE_WATCHDOG}.service")
    assert "OnUnitActiveSec=10s" in store.read_text(timer)
    assert "watchdog devices" in store.read_text(service)
    assert f"systemctl enable --now {DEVICE_WATCHDOG}.timer" in ctx.runner.changes()


# --- client add, remove, rename ---

def test_add_counts_down_without_wgt_ctl(make_ctx, capsys):
    ctx = make_ctx(argv=["--exit", "host"])
    clients.cmd_add(ctx, ["ipad"])
    assert numbers(ctx) == {"ipad": 254}
    assert "10.9.8.254" in capsys.readouterr().out
    clients.cmd_add(ctx, ["pixel"])
    assert numbers(ctx) == {"ipad": 254, "pixel": 253}


def test_add_takes_the_fleets_number(make_ctx):
    ctx = make_ctx(argv=["--exit", "host", "--device-number", "9"])
    clients.cmd_add(ctx, ["ipad"])
    assert numbers(ctx) == {"ipad": 9}


def test_another_config_of_a_device_keeps_its_number(make_ctx, again):
    ctx = make_ctx(argv=["--exit", "host", "--warp", "on"])
    clients.cmd_add(ctx, ["ipad"])
    ctx = again(ctx, ["--exit", "par-fr", "--warp", "on"])
    clients.cmd_add(ctx, ["ipad"])
    assert numbers(ctx) == {"ipad": 254}
    assert len(ctx.snap.device_clients("ipad")) == 2


def test_add_warns_when_the_fleets_number_differs_or_is_taken(make_ctx, again, capsys):
    ctx = make_ctx(argv=["--exit", "host", "--warp", "on", "--device-number", "9"])
    clients.cmd_add(ctx, ["ipad"])
    ctx = again(ctx, ["--exit", "host", "--warp", "off", "--device-number", "10"])
    clients.cmd_add(ctx, ["ipad"])
    assert numbers(ctx) == {"ipad": 9}
    ctx = again(ctx, ["--exit", "host", "--device-number", "9"])
    clients.cmd_add(ctx, ["pixel"])
    assert numbers(ctx) == {"ipad": 9, "pixel": 254}
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "not 10.9.8.10" in text and "10.9.8.9 is 'ipad'" in text


@pytest.mark.parametrize("word", ["1", "255", "x"])
def test_add_refuses_a_bad_number_before_changing_anything(make_ctx, word):
    ctx = make_ctx(argv=["--exit", "host", "--device-number", word])
    with pytest.raises(WgtError, match="device number"):
        clients.cmd_add(ctx, ["ipad"])
    assert not ctx.snap.device_clients("ipad")


def test_dry_run_add_writes_no_number(make_ctx):
    ctx = make_ctx(argv=["--exit", "host", "--dry-run"])
    clients.cmd_add(ctx, ["ipad"])
    assert store.read_text(ctx.paths.device_db) is None


def test_number_goes_with_the_last_config(make_ctx):
    ctx = make_ctx(argv=["--exit", "host"])
    clients.cmd_add(ctx, ["ipad"])
    clients.cmd_remove(ctx, ["warp.ipad"])
    assert numbers(ctx) == {"ipad": 254}
    clients.cmd_remove(ctx, ["ipad"])
    assert numbers(ctx) == {}


def test_renaming_a_config_within_its_device_keeps_the_number(make_ctx):
    ctx = make_ctx(argv=["--exit", "host"])
    clients.cmd_add(ctx, ["ipad"])
    clients.cmd_rename(ctx, ["warp.ipad", "warp.par-fr.ipad"])
    assert numbers(ctx) == {"ipad": 254}


def test_renaming_a_devices_last_config_moves_the_number(make_ctx):
    ctx = make_ctx(argv=["--exit", "host", "--warp", "on"])
    clients.cmd_add(ctx, ["ipad"])
    clients.cmd_rename(ctx, ["warp.ipad", "warp.tablet"])
    assert numbers(ctx) == {"tablet": 254}


def test_renaming_one_of_several_configs_numbers_the_new_device(make_ctx, again):
    ctx = make_ctx(argv=["--exit", "host"])
    clients.cmd_add(ctx, ["ipad"])
    ctx = again(ctx)
    clients.cmd_rename(ctx, ["warp.ipad", "warp.tablet"])
    assert numbers(ctx) == {"ipad": 254, "tablet": 253}


# --- wgt device ---

def test_device_rename_moves_every_config_and_the_number(make_ctx):
    ctx = make_ctx(argv=["--exit", "host,par-fr"])
    clients.cmd_add(ctx, ["ipad"])
    ips = {c.name: c.ip for c in ctx.snap.device_clients("ipad")}
    assert sorted(ips) == ["ipad", "par-fr.ipad", "warp.ipad", "warp.par-fr.ipad"]
    clients.cmd_device_rename(ctx, ["ipad", "tablet"])
    assert numbers(ctx) == {"tablet": 254}
    assert not ctx.snap.device_clients("ipad")
    assert {c.name: c.ip for c in ctx.snap.device_clients("tablet")} == {
        name.replace("ipad", "tablet"): ip for name, ip in ips.items()}


def test_device_rename_renames_nothing_with_a_config_file_missing(make_ctx):
    ctx = make_ctx()    # the fixture's clients have no config files
    with pytest.raises(WgtError, match="nothing renamed"):
        clients.cmd_device_rename(ctx, ["mbp-1", "mac"])
    assert len(ctx.snap.device_clients("mbp-1")) == 2


def test_device_rename_refusals(make_ctx):
    ctx = make_ctx()
    with pytest.raises(WgtError, match="no device"):
        clients.cmd_device_rename(ctx, ["nope", "x"])
    with pytest.raises(WgtError, match="already exists"):
        clients.cmd_device_rename(ctx, ["mbp-1", "tv"])
    with pytest.raises(WgtError, match="no prefix"):
        clients.cmd_device_rename(ctx, ["warp.mbp-1", "warp.x"])


def test_device_set(make_ctx):
    ctx = make_ctx()
    devices.cmd_set(ctx, ["tv", "12"])
    assert numbers(ctx) == {"tv": 12}
    devices.cmd_set(ctx, ["mbp-1"])
    assert numbers(ctx) == {"tv": 12, "mbp-1": 254}
    with pytest.raises(WgtError, match="already 'tv'"):
        devices.cmd_set(ctx, ["mbp-1", "12"])
    with pytest.raises(WgtError, match="no device"):
        devices.cmd_set(ctx, ["nope", "13"])
    with pytest.raises(WgtError, match="device number"):
        devices.cmd_set(ctx, ["tv", "1"])


def test_device_list_tsv_is_what_wgt_ctl_reads(make_ctx, again, capsys):
    ctx = make_ctx()
    devices.cmd_set(ctx, ["tv", "12"])
    ctx = again(ctx, ["--tsv"])
    capsys.readouterr()
    devices.cmd_list(ctx, [])
    assert capsys.readouterr().out.splitlines() == ["mbp-1\t", "phone\t", "tv\t12"]


def test_client_table_shows_the_static_address(make_ctx):
    ctx = make_ctx()
    devices.cmd_set(ctx, ["tv", "12"])
    row = next(r for r in clients.client_rows(ctx) if r.name == "warp.par-fr.tv")
    assert row.static == "10.9.8.12"
    assert next(r for r in clients.client_rows(ctx) if r.name == "mbp-1").static == ""


# --- the watchdog ---

def numbered(ctx):
    """mbp-1 is number 4; it has two configs."""
    devices.cmd_set(ctx, ["mbp-1", "4"])
    return ctx.snap.client("mbp-1"), ctx.snap.client("warp.par-fr.mbp-1")


def test_desired_points_at_the_newest_recent_config(make_ctx):
    ctx = make_ctx()
    plain, warped = numbered(ctx)
    forwards, sources = devices.desired(ctx.snap, {plain.pub: NOW - 100, warped.pub: NOW - 5}, NOW)
    assert forwards == {"10.9.8.4": f"-d 10.9.8.4/32 -j DNAT --to-destination {warped.ip}"}
    assert sources == [f"-s {plain.ip}/32 -j SNAT --to-source 10.9.8.4",
                       f"-s {warped.ip}/32 -j SNAT --to-source 10.9.8.4"]


@pytest.mark.parametrize("age", [181, 100000])
def test_desired_has_no_forward_without_a_recent_handshake(make_ctx, age):
    ctx = make_ctx()
    plain, _ = numbered(ctx)
    assert devices.desired(ctx.snap, {plain.pub: NOW - age}, NOW)[0] == {}
    assert devices.desired(ctx.snap, {plain.pub: 0}, NOW)[0] == {}


def test_desired_skips_disabled_configs(make_ctx):
    ctx = make_ctx()
    plain, warped = numbered(ctx)
    clients.cmd_disable(ctx, ["warp.par-fr.mbp-1"])
    forwards, sources = devices.desired(ctx.snap, {plain.pub: NOW - 50, warped.pub: NOW - 5}, NOW)
    assert forwards["10.9.8.4"].endswith(plain.ip)
    assert sources == [f"-s {plain.ip}/32 -j SNAT --to-source 10.9.8.4"]


def chain_answer(chain, specs=()):
    return (0, "".join([f"-N {chain}\n"] + [f"-A {chain} {s}\n" for s in specs]))


def changes_to(ctx, chain):
    return [c for c in ctx.runner.calls if f"-t nat -A {chain}" in c or f"-t nat -D {chain}" in c]


def test_sync_adds_the_new_forward_before_removing_the_old(make_ctx):
    ctx = make_ctx()
    plain, warped = numbered(ctx)
    old = f"-d 10.9.8.4/32 -j DNAT --to-destination {plain.ip}"
    ctx.runner.answers.update({
        "wg show wg0 latest-handshakes": (0, f"{warped.pub}\t{NOW - 5}\n"),
        f"iptables -t nat -S {DEVICE_CHAIN}": chain_answer(DEVICE_CHAIN, [old]),
        f"iptables -t nat -S {DEVICE_SOURCE_CHAIN}": chain_answer(DEVICE_SOURCE_CHAIN),
    })
    log = devices.sync(ctx)
    assert changes_to(ctx, DEVICE_CHAIN) == [
        f"iptables -t nat -A {DEVICE_CHAIN} -d 10.9.8.4/32 -j DNAT --to-destination {warped.ip}",
        f"iptables -t nat -D {DEVICE_CHAIN} {old}"]
    assert len(changes_to(ctx, DEVICE_SOURCE_CHAIN)) == 2
    assert log == [f"10.9.8.4 -> {warped.ip}"]


def test_sync_changes_nothing_when_right(make_ctx):
    ctx = make_ctx()
    plain, warped = numbered(ctx)
    ctx.runner.answers.update({
        "wg show wg0 latest-handshakes": (0, f"{plain.pub}\t{NOW - 5}\n"),
        f"iptables -t nat -S {DEVICE_CHAIN}": chain_answer(
            DEVICE_CHAIN, [f"-d 10.9.8.4/32 -j DNAT --to-destination {plain.ip}"]),
        f"iptables -t nat -S {DEVICE_SOURCE_CHAIN}": chain_answer(DEVICE_SOURCE_CHAIN, [
            f"-s {plain.ip}/32 -j SNAT --to-source 10.9.8.4",
            f"-s {warped.ip}/32 -j SNAT --to-source 10.9.8.4"]),
    })
    assert devices.sync(ctx) == []
    assert not changes_to(ctx, DEVICE_CHAIN) and not changes_to(ctx, DEVICE_SOURCE_CHAIN)


def test_sync_logs_a_device_going_quiet(make_ctx):
    ctx = make_ctx()
    plain, _ = numbered(ctx)
    ctx.runner.answers.update({
        "wg show wg0 latest-handshakes": (0, ""),
        f"iptables -t nat -S {DEVICE_CHAIN}": chain_answer(
            DEVICE_CHAIN, [f"-d 10.9.8.4/32 -j DNAT --to-destination {plain.ip}"]),
    })
    assert devices.sync(ctx) == ["10.9.8.4: no config in use"]


def test_sync_waits_for_the_chains(make_ctx):
    """Updated but not set up yet: wg0 has no chains, and that's not an error."""
    ctx = make_ctx()
    numbered(ctx)
    assert devices.sync(ctx) == []
    assert not any(" -A WGT-" in c for c in ctx.runner.calls)


def test_watchdog_logs_to_its_own_tag_and_skips_while_locked(make_ctx, monkeypatch):
    ctx = make_ctx(bare=True, has_ipv6=False)
    monkeypatch.setattr(devices, "sync", lambda c: ["10.9.8.4: no config in use"])
    watchdog.cmd_watchdog(ctx, ["devices"])
    assert any(DEVICE_WATCHDOG in c and "no config in use" in c for c in ctx.runner.calls)
    ctx.runner.calls.clear()
    monkeypatch.setattr(watchdog.ops, "try_lock", lambda c: False)
    watchdog.cmd_watchdog(ctx, ["devices"])
    assert ctx.runner.calls == []


# --- the fleet's record ---

def test_fleet_file_round_trip_sorted_by_number():
    entries = {"b": Entry(9), "a": Entry(2, "2026-01-01")}
    text = fleet.format_entries(entries)
    assert text.splitlines()[1:] == ["a 2 removed 2026-01-01", "b 9"]
    assert fleet.parse(text) == entries
    assert fleet.parse(None) == {}


@pytest.mark.parametrize("line", ["a", "a x", "a 2 removed", "a 2 gone 2026-01-01"])
def test_fleet_bad_lines_refused(line):
    with pytest.raises(WgtError, match="local/devices"):
        fleet.parse(line)


def test_merge_adds_clears_and_flags():
    before = {"back": Entry(3, "2026-01-01"), "gone": Entry(4)}
    seen = {"s1": {"new": 5, "back": 3, "none": None}}
    got = fleet.merge(before, seen, True, "2026-11-06")
    assert got == {"back": Entry(3), "gone": Entry(4, "2026-11-06"), "new": Entry(5)}
    assert before["gone"].removed == ""     # the input is left alone


def test_merge_flags_nothing_with_a_server_missing():
    assert fleet.merge({"gone": Entry(4)}, {"s1": {}}, False, "2026-11-06")["gone"].removed == ""


def test_next_low_never_reuses_and_runs_out():
    assert fleet.next_low({}) == 2
    assert fleet.next_low({"a": Entry(2), "b": Entry(7, "2026-01-01"), "direct": Entry(254)}) == 8
    with pytest.raises(WgtError, match="no static numbers left"):
        fleet.next_low({"a": Entry(127)})


def test_problems_says_what_to_run():
    entries = {"a": Entry(5), "b": Entry(5), "c": Entry(6)}
    found = fleet.problems(entries, {"s1": {"c": 7, "d": None}})
    assert any("10.9.8.5 is claimed by a and b" in p for p in found)
    assert any("c is 10.9.8.7 on s1" in p and "devices fix c 6" in p for p in found)
    assert any("d has no static address on s1" in p for p in found)
    assert fleet.problems({"a": Entry(5)}, {"s1": {"a": 5}}) == []


def test_device_ip():
    assert device_ip(4) == "10.9.8.4"


# --- groups ---

def _in_groups(ctx, **groups):
    """<ctx> with every config enabled (the fixture disables tv's), and devices in groups."""
    with open(ctx.paths.disabled_db, "w", encoding="utf-8"):
        pass
    ctx.reload()
    for device, word in groups.items():
        if ctx.snap.device_number(device) is None:
            devices.cmd_set(ctx, [device])
        devices.cmd_group(ctx, [device, word])
    return ctx


def test_no_groups_means_isolated(make_ctx):
    ctx = make_ctx()
    devices.cmd_set(ctx, ["mbp-1", "4"])
    devices.cmd_set(ctx, ["tv", "5"])
    assert devices.peer_rules(ctx.snap) == [] and devices.server_sources(ctx.snap) == []


def test_sharing_a_group_allows_static_traffic_both_ways_only(make_ctx):
    ctx = _in_groups(make_ctx(), tv="home")
    devices.cmd_set(ctx, ["mbp-1", "4"])
    assert devices.peer_rules(ctx.snap) == []
    devices.cmd_group(ctx, ["mbp-1", "home"])
    found = devices.peer_rules(ctx.snap)
    tv = ctx.snap.client("warp.par-fr.tv").ip
    mac = ctx.snap.client("mbp-1").ip
    assert f"-s {tv}/32 -d {mac}/32 -m conntrack --ctstate DNAT -j ACCEPT" in found
    assert f"-s {mac}/32 -d {tv}/32 -m conntrack --ctstate DNAT -j ACCEPT" in found
    assert all("--ctstate DNAT" in rule for rule in found)


def test_a_device_in_two_groups_bridges_nothing(make_ctx):
    ctx = _in_groups(make_ctx(), tv="home", phone="work")
    devices.cmd_set(ctx, ["mbp-1", "4"])
    devices.cmd_group(ctx, ["mbp-1", "home,work"])
    tv = ctx.snap.client("warp.par-fr.tv").ip
    phone = ctx.snap.client("nyc-us.phone").ip
    found = " ".join(devices.peer_rules(ctx.snap))
    assert f"-s {tv}/32 -d {phone}/32" not in found and f"-s {phone}/32 -d {tv}/32" not in found
    assert f"-s {tv}/32" in found and f"-s {phone}/32" in found


def test_the_server_group_reaches_the_server_not_each_other(make_ctx):
    ctx = _in_groups(make_ctx(), tv="server", phone="server")
    assert devices.peer_rules(ctx.snap) == []
    assert {c.name for c in devices.server_sources(ctx.snap)} == {"warp.par-fr.tv", "nyc-us.phone"}


def test_ungroup_one_or_all(make_ctx):
    ctx = _in_groups(make_ctx(), tv="home,work,server")
    devices.cmd_ungroup(ctx, ["tv", "work"])
    assert ctx.snap.device_groups("tv") == ("home", "server")
    devices.cmd_ungroup(ctx, ["tv"])
    assert ctx.snap.device_groups("tv") == ()
    assert ctx.snap.device_number("tv") is not None


def test_client_add_takes_groups_and_they_survive_a_new_number_and_a_rename(make_ctx):
    ctx = make_ctx(argv=["--group", "home,server"])
    clients.cmd_add(ctx, ["ipad"])
    assert ctx.snap.device_groups("ipad") == ("home", "server")
    devices.cmd_set(ctx, ["ipad", "77"])
    assert ctx.snap.device_groups("ipad") == ("home", "server")
    clients.cmd_device_rename(ctx, ["ipad", "tablet"])
    assert ctx.snap.device_groups("tablet") == ("home", "server") and ctx.snap.device_number("tablet") == 77


def test_a_new_device_starts_isolated(make_ctx):
    ctx = make_ctx()
    clients.cmd_add(ctx, ["ipad"])
    assert ctx.snap.device_groups("ipad") == ()


def test_bad_group_names_are_refused():
    for word in ("", ",", "a b", "-x", "a" * 33):
        with pytest.raises(WgtError):
            store.parse_groups(word)
    assert store.parse_groups("home, work,home") == ("home", "work")


def test_devices_db_keeps_groups():
    text = store.put_device(None, "tv", 5, ("home", "server"))
    assert store.parse_devices(text)[0] == store.Device("tv", 5, ("home", "server"))
    assert store.parse_devices(store.put_device(text, "tv", 6))[0].groups == ("home", "server")
    assert text.splitlines()[1] == "tv 5 home,server"


def test_tsv_gives_groups_as_a_third_column(make_ctx, again, capsys):
    ctx = _in_groups(make_ctx(), tv="home,server")
    capsys.readouterr()
    devices.cmd_list(again(ctx, ["--tsv"]), [])
    assert any(line.split("\t")[0] == "tv" and line.split("\t")[2:] == ["home,server"]
               for line in capsys.readouterr().out.splitlines())


def test_sync_inserts_allowed_above_the_refusal_and_removes_stale(make_ctx):
    ctx = _in_groups(make_ctx(), tv="server")
    tv = ctx.snap.client("warp.par-fr.tv").ip
    r = ctx.runner
    r.answers["iptables -t nat -S WGT-DEVICES"] = (0, "")
    r.answers["iptables -t filter -S WGT-PEERS"] = (0, "-N WGT-PEERS\n-A WGT-PEERS -s 1.2.3.4/32 -j ACCEPT\n"
                                                       "-A WGT-PEERS -j REJECT --reject-with icmp-port-unreachable\n")
    r.answers["iptables -t filter -S WGT-SERVER"] = (0, "-N WGT-SERVER\n"
                                                        "-A WGT-SERVER -j REJECT --reject-with icmp-port-unreachable\n")
    devices.sync(ctx)
    run = r.calls
    assert f"iptables -t filter -I WGT-SERVER -s {tv}/32 -j ACCEPT" in run
    assert "iptables -t filter -D WGT-PEERS -s 1.2.3.4/32 -j ACCEPT" in run
    assert not any("-D WGT-PEERS -j REJECT" in c or "-D WGT-SERVER -j REJECT" in c for c in run)
