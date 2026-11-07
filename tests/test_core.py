"""Unit tests for the pure helpers: names, addresses, settings and the server files."""

from __future__ import annotations

import os
import random

import pytest
from conftest import ROOT

from wgt import net, store
from wgt.config import resolve, short_hostname_name
from wgt.errors import WgtError

# --- names ---

@pytest.mark.parametrize("code,ok", [
    ("par-fr", True), ("nyc", True), ("ab", True), ("a", False), ("par-fra", False),
    ("par-", False), ("1ab", False), ("warp", False), ("Par", False), ("pa_r", False),
])
def test_exit_codes(code, ok):
    assert store.valid_exit_code(code) is ok


@pytest.mark.parametrize("name,ok", [
    ("laptop", True), ("par-fr.laptop", True), ("Work_PC-2", True), ("x" * 32, True),
    ("warp.laptop", True), ("warp.par-fr.laptop", True), ("warp", True),
    ("x" * 33, False), ("par-fr.warp.laptop", False), ("bad code.x", False), ("a.b.c", False),
    ("warp.warp.x", False), ("warp.par-fr.", False), ("", False),
])
def test_client_names(name, ok):
    assert (store.valid_client_name(name) is None) is ok


def test_client_exit():
    assert store.client_exit("par-fr.laptop") == "par-fr"
    assert store.client_exit("warp.par-fr.laptop") == "par-fr"
    assert store.client_exit("warp.laptop") == ""
    assert store.client_exit("laptop") == ""


def test_split_and_build_names():
    for name, parts in [("mac", ("", False, "mac")), ("warp.mac", ("", True, "mac")),
                        ("par-fr.mac", ("par-fr", False, "mac")), ("warp.par-fr.mac", ("par-fr", True, "mac"))]:
        assert store.split_name(name) == parts
        assert store.config_name(*parts) == name
    assert store.device_of("warp.par-fr.mac") == "mac"
    order = sorted(["warp.par-fr.mac", "warp.mac", "par-fr.mac", "mac"], key=store.device_key)
    assert order == ["mac", "warp.mac", "par-fr.mac", "warp.par-fr.mac"]


# --- addresses ---

def test_ip_ints():
    assert net.ip2int("10.2.10.3") == 0x0A020A03
    assert net.int2ip(0x0A020A03) == "10.2.10.3"


@pytest.mark.parametrize("value,want", [
    ("FDAA:08F8:C196::/64", "fdaa:8f8:c196::/64"),
    ("fd00:0:0::/64", "fd00::/64"),
    ("fdaa::/64", "fdaa::/64"),
    ("fdaa:1:2:3:4::/64", None),
    ("fdaa::/48", None),
])
def test_canon_subnet6(value, want):
    assert net.canon_subnet6(value) == want


def test_random_subnet_avoids_routes():
    # Half of the candidates are taken; the pick (20 tries) avoids them.
    routes = "\n".join(f"10.{a}.{b}.0/24 dev x" for a in range(200, 228) for b in range(200, 256))
    for seed in range(50):
        s = net.random_subnet(routes, random.Random(seed))
        assert int(s.split(".")[1]) >= 228, s


def test_picked_subnets_stay_in_their_range():
    # high in 10.x, so never the device network or a server subnet like 10.8.0.0/24
    for seed in range(200):
        _, a, b, _ = net.random_subnet("", random.Random(seed)).split(".")
        assert 200 <= int(a) <= 255 and 200 <= int(b) <= 255


def test_random_subnet6():
    s = net.random_subnet6()
    assert s.startswith("fd") and s.endswith("::/64")


# --- settings ---

def cfg(**saved):
    base = {"WG_SUBNET": "10.4.5.0/24", "WG_SUBNET6": "fd12:3456:789a::/64", "SERVER_NAME": "t"}
    base.update(saved)
    return resolve({}, {}, base)


def test_precedence():
    c = resolve({"WG_PORT": "1"}, {"WG_PORT": "2", "WG_DNS": "9.9.9.9"},
                {"WG_PORT": "3", "WG_DNS": "8.8.8.8", "WG_MTU": "1300", "WG_SUBNET": "10.1.1.0/24"})
    assert (c.WG_PORT, c.sources["WG_PORT"]) == ("1", "flag")
    assert (c.WG_DNS, c.sources["WG_DNS"]) == ("9.9.9.9", "env")
    assert (c.WG_MTU, c.sources["WG_MTU"]) == ("1300", "saved")
    assert not c.subnet_defaulted


def test_random_subnet_when_none():
    c = resolve({}, {}, {})
    assert c.subnet_defaulted and c.sources["WG_SUBNET"] == "random"


def test_hostname_default():
    assert short_hostname_name("dengar.r2unit.com") == "dengar"
    assert short_hostname_name("racknerd a063") == "racknerd-a063"
    assert short_hostname_name("") == "wg"


def test_finalize_derives():
    c = cfg()
    c.finalize(kernel_ipv6=True)
    assert (c.server_ip, c.pfx, c.ipv6) == ("10.4.5.1", 24, True)
    assert c.client_addrs("10.4.5.12") == "10.4.5.12/32,fd12:3456:789a::c/128"
    c.finalize(kernel_ipv6=False)
    assert c.client_addrs("10.4.5.12") == "10.4.5.12/32"


@pytest.mark.parametrize("saved,err", [
    ({"WG_SUBNET": "10.4.5.1/24"}, "not a network address (did you mean 10.4.5.0/24?)"),
    ({"WG_SUBNET": "10.4.5.0/31"}, "prefix must be /16 to /30"),
    ({"WG_MTU": "1500", "WARP_MTU": "1280"}, "must not exceed WARP_MTU"),
    ({"RT_TABLE": "205"}, "reserved for jumps"),
    ({"WG_IF": "warp"}, "must differ"),
    ({"WG_SUBNET6": "fdaa::/48"}, "bad --subnet6"),
    ({"SERVER_NAME": "a.b"}, "server name must be"),
])
def test_finalize_rejects(saved, err):
    c = cfg(**saved)
    with pytest.raises(WgtError, match=err.replace("(", r"\(").replace(")", r"\)").replace("?", r"\?")):
        c.finalize(kernel_ipv6=True)


# --- server files ---

def test_state():
    t = store.state_set("A=1\nB=2\n", "A", "3")
    assert t == "B=2\nA=3\n"
    assert store.state_get("A=1\nA=2\n", "A") == "2"
    assert store.state_unset("A=1\n", "A") == ""


def test_clients_db_keeps_other_lines():
    text = store.CLIENTS_HEADER + "\nb  10.0.0.3   KEYB  0.0.0.0/0\na 10.0.0.2 KEYA 0.0.0.0/0\n"
    out = store.put_client(text, "a", store.Client("c", "10.0.0.4", "KEYC", "0.0.0.0/0"))
    assert out == store.CLIENTS_HEADER + "\nb  10.0.0.3   KEYB  0.0.0.0/0\nc 10.0.0.4 KEYC 0.0.0.0/0\n"


def test_exits_db():
    text = store.put_exit(None, "par-fr", store.Exit("par-fr", 1, "Paris, France"))
    assert store.parse_exits(text) == [store.Exit("par-fr", 1, "Paris, France")]
    assert store.parse_exits(store.put_exit(text, "par-fr")) == []


def test_disabled():
    assert store.set_disabled("a\nb\n", "a", False) == "b\n"
    assert store.set_disabled("b\n", "a", True) == "b\na\n"


def test_backup_record():
    t = store.backup_record(None, "/x", "NEW")
    assert store.backup_record(t, "/x", "/copy") == t       # first record wins
    assert store.backup_orig(t, "/x") == "NEW"
    assert store.backup_forget(t, "/x") == ""


def test_snapshot_exit_clients():
    db = "a 10.0.0.2 k\npar-fr.a 10.0.0.3 k\npar-fr2.b 10.0.0.4 k\n"
    snap = store.Snapshot(None, store.parse_clients(db), [], [])
    assert [c.name for c in snap.exit_clients("par-fr")] == ["par-fr.a"]


@pytest.mark.parametrize("name", ["bob\n", "wg0\n"])
def test_trailing_newline_rejected(name):
    assert store.valid_client_name(name) is not None
    c = cfg(WG_IF=name)
    with pytest.raises(WgtError):
        c.finalize(kernel_ipv6=True)


def test_non_ascii_digits_rejected():
    with pytest.raises(WgtError, match="bad port"):
        cfg(WG_PORT="٥١٨٢٠").finalize(kernel_ipv6=True)


def test_ip6_of_outside_subnet():
    c = cfg()
    c.finalize(kernel_ipv6=True)
    with pytest.raises(WgtError):
        c.ip6_of("10.9.9.9")


@pytest.mark.parametrize("route,dev", [
    ("default via 1.2.3.1 dev eth0 proto static", "eth0"),
    ("default dev venet0 scope link", "venet0"),
    ("default via 1.2.3.1 dev ens3 metric 100", "ens3"),
])
def test_route_dev(route, dev):
    from wgt import host
    assert host.route_dev(route) == dev


def test_ipt_count_matches_iptables_s():
    from wgt import live
    from wgt.system import FakeRunner
    r = FakeRunner({"iptables -t filter -S INPUT": (0, "-A INPUT -p udp -m udp --dport 51820 -j ACCEPT\n"
                                                         "-A INPUT -p udp -m udp --dport 51820 -j ACCEPT\n")})
    assert live.ipt_count(r, "iptables", "filter", "INPUT", "-p udp --dport 51820 -j ACCEPT") == 2


def test_ask_reads_the_terminal():
    """ui.ask in a real (pseudo) terminal: the prompt shows and the reply comes back."""
    import pty
    import sys

    code = (f"import sys; sys.path.insert(0, {ROOT!r}); from wgt import ui; "
            "print('GOT=' + ui.ask('Which? '))")
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(sys.executable, [sys.executable, "-c", code])
    os.write(fd, b"2\n")
    out = b""
    while True:
        try:
            chunk = os.read(fd, 1024)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
    os.waitpid(pid, 0)
    text = out.decode()
    assert "Which? " in text and "GOT=2" in text, text


def test_host_is_not_an_exit_code():
    assert not store.valid_exit_code("host") and not store.valid_exit_code("warp")
    assert store.valid_client_name("host.mac") is not None   # an error: host is not a jump


def test_client_table_per_cell():
    from wgt import rules
    snap = store.Snapshot(state="", clients=[store.Client("warp.a", "10.0.0.2", "K"),
                                            store.Client("warp.par-fr.a", "10.0.0.3", "K"),
                                            store.Client("a", "10.0.0.4", "K"),
                                            store.Client("par-fr.a", "10.0.0.5", "K")],
                          disabled=[], exits=[store.Exit("par-fr", 1, "Paris")])
    host_w, city_w, host, city = snap.clients
    assert rules.client_table(snap, host_w) == "" and rules.client_table(snap, city_w) == "201"
    assert rules.client_table(snap, host) == "main" and rules.client_table(snap, city) == "231"
    assert [c.name for c in snap.direct_clients()] == ["a"]


def test_next_free_subnet():
    # in order, skipping the fleet's subnets (any size) and the server's own routes
    assert net.next_free_subnet("", []) == "10.200.200.0/24"
    assert net.next_free_subnet("", ["10.200.200.0/24", "10.200.201.0/24"]) == "10.200.202.0/24"
    assert net.next_free_subnet("10.200.200.0/24 dev eth1", ["10.200.202.0/23"]) == "10.200.201.0/24"
    assert net.next_free_subnet("", ["192.168.250.0/24", "junk"]) == "10.200.200.0/24"


def test_fleet_subnets_pick_next_free():
    c = resolve({}, {"WGT_FLEET_SUBNETS": "10.200.200.0/24,10.2.10.0/24"}, {})
    assert c.subnet_defaulted and c.values["WG_SUBNET"] == "10.200.201.0/24"
    assert c.sources["WG_SUBNET"] == "next free in the fleet"
    saved = resolve({}, {"WGT_FLEET_SUBNETS": "10.1.1.0/24"}, {"WG_SUBNET": "10.9.9.0/24"})
    assert saved.values["WG_SUBNET"] == "10.9.9.0/24"   # a saved subnet stays


# --- the databases and settings refuse what would go wrong later ---

@pytest.mark.parametrize("text", ["a 5\nb 5\n", "a 5\na 6\n", "a ²\n"])
def test_devices_db_refuses_duplicates_and_odd_digits(text):
    with pytest.raises(WgtError, match="devices.db"):
        store.parse_devices(text)


def test_clients_db_refuses_an_impossible_address():
    with pytest.raises(WgtError, match="clients.db"):
        store.parse_clients("phone 999.1.1.1 KEY\n")


@pytest.mark.parametrize("key,value", [
    ("WG_DNS", "1.1.1.1\nPostUp = touch /x"), ("CLIENT_ALLOWED_IPS", "10.0.0.0/8\nPostUp = touch /x"),
    ("CLIENT_ALLOWED_IPS", "not-a-cidr"), ("ENDPOINT", "vpn.example.com:51820"), ("ENDPOINT", "1.2.3.4:51820"),
])
def test_values_sent_to_devices_are_checked(key, value):
    c = resolve({key: value}, {}, {"WG_SUBNET": "10.1.1.0/24"})
    with pytest.raises(WgtError):
        c.finalize(kernel_ipv6=False)


@pytest.mark.parametrize("key,value", [
    ("WG_DNS", "1.1.1.1, 2606:4700:4700::1111"), ("WG_DNS", "9.9.9.9,home.lan"), ("ENDPOINT", "vpn.example.com"),
    ("ENDPOINT", "[2001:db8::1]"), ("ENDPOINT", "2001:db8::1"), ("ENDPOINT", "203.0.113.1"),
    ("CLIENT_ALLOWED_IPS", "0.0.0.0/0, ::/0"), ("CLIENT_ALLOWED_IPS", "10.4.5.0/24,fd00::/64"),
])
def test_values_sent_to_devices_can_be_normal(key, value):
    resolve({key: value}, {}, {"WG_SUBNET": "10.1.1.0/24"}).finalize(kernel_ipv6=False)


def test_a_terminal_gone_at_a_question_is_no_even_when_enter_means_yes(monkeypatch):
    """Otherwise a lost session renames the host, or brings an old server back."""
    import builtins

    from wgt import ui

    def gone(*a, **k):
        raise OSError("no terminal")
    monkeypatch.setattr(builtins, "open", gone)
    assert ui.yes("Rename?", default=True) is False


@pytest.mark.parametrize("line, want", [("", False), ("\n", True), ("y\n", True), ("n\n", False)])
def test_eof_isnt_enter(monkeypatch, line, want):
    import builtins
    import io

    from wgt import ui
    monkeypatch.setattr(builtins, "open", lambda path, mode="r": io.StringIO(line if mode == "r" else ""))
    assert ui.yes("Rename?", default=True) is want

