"""Everything wg0.conf, warp.conf and the exit tunnels are built from must match
what the bash version rendered for the same server (tests/golden, made by
tools/golden-bash.sh): switching tools must rewrite nothing on a live server."""

from __future__ import annotations

import os

from conftest import golden, golden_names

from wgt import render, rules, store


def lines(xs):
    return "".join(x + "\n" for x in xs)


def test_wg0_conf(server):
    got = render.render_wg_conf(server.cfg, server.snap, server.pub_if, store.psk_reader(server.paths))
    assert server.norm(got) == golden(server.case, "wg0.conf")


def test_warp_conf(server):
    profile = open(server.paths.wg("wgcf", "wgcf-profile.conf")).read()
    got = render.render_warp_conf(server.cfg, profile, has_ipv6=True, resolve4=lambda h: None)
    assert got == golden(server.case, "warp.conf")


def test_hooks(server):
    assert server.norm(lines(rules.hook_cmds(server.cfg, server.snap, server.pub_if, "up"))) == golden(server.case, "hooks-up.txt")
    assert server.norm(lines(rules.hook_cmds(server.cfg, server.snap, server.pub_if, "down"))) == golden(server.case, "hooks-down.txt")


def test_ipt_rules(server):
    got = lines("|".join(r) for r in rules.ipt_rules(server.cfg, server.pub_if, server.snap.exits))
    assert got == golden(server.case, "ipt-rules.txt")


def test_warp_switch(server):
    assert lines(rules.warp_rule_cmds(server.cfg, True)) == golden(server.case, "warp-on.txt")
    assert lines(rules.warp_rule_cmds(server.cfg, False)) == golden(server.case, "warp-off.txt")


def test_dropin(server):
    assert render.render_dropin(server.cfg) == golden(server.case, "dropin.conf")


def test_exits(server):
    for e in server.snap.exits:
        raw = open(server.paths.wg("exits", f"{e.code}.proton.conf")).read()
        assert render.render_proton_conf(raw, e) == golden(server.case, f"px-{e.code}.conf")
        profile = open(server.paths.wg("exits", e.code, "wgcf-profile.conf")).read()
        wx = render.render_warp_conf(server.cfg, profile, has_ipv6=True, resolve4=lambda h: None,
                                     table=rules.exit_wtable(e), mark=rules.exit_mark(e))
        assert wx == golden(server.case, f"wx-{e.code}.conf")
        assert lines(rules.exit_hook_cmds(server.cfg, server.snap, e, "up")) == golden(server.case, f"exit-{e.code}-up.txt")
        assert lines(rules.exit_hook_cmds(server.cfg, server.snap, e, "down")) == golden(server.case, f"exit-{e.code}-down.txt")
        got = lines("|".join(r) for r in rules.ipt_rules_out(server.cfg, f"wx-{e.code}"))
        assert got == golden(server.case, f"exit-{e.code}-ipt.txt")


def test_client_conf(server):
    name = next(n for n in golden_names(server.case) if n.startswith("client-"))[len("client-"):-len(".conf")]
    c = server.snap.client(name)
    got = render.render_client_conf(server.cfg, server.snap, name, c.ip, "CLIENT-PRIVATE-KEY",
                                    "SERVER-PUBLIC-KEY", "CLIENT-PSK", "ENDPOINT", "0.0.0.0/0,::/0")
    assert got == golden(server.case, f"client-{name}.conf")


def test_every_golden_file_is_checked(server):
    """A new golden file must come with a test."""
    known = {"wg0.conf", "warp.conf", "hooks-up.txt", "hooks-down.txt", "ipt-rules.txt",
             "warp-on.txt", "warp-off.txt", "dropin.conf"}
    for e in server.snap.exits:
        known |= {f"px-{e.code}.conf", f"wx-{e.code}.conf", f"exit-{e.code}-up.txt",
                  f"exit-{e.code}-down.txt", f"exit-{e.code}-ipt.txt"}
    for n in golden_names(server.case):
        if n in known or n.startswith("client-"):
            continue
        raise AssertionError(f"golden file {server.case}/{n} is checked by no test (stale?)")
    assert os.path.isdir(server.wg_dir)
