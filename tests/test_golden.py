"""Everything wg0.conf, warp.conf and the exit tunnels are built from must match
tests/golden byte for byte, because a change to them restarts wg0 on every server
at its next setup. After a deliberate change, regenerate them with UPDATE_GOLDEN=1
and review the diff (see DEVELOPING.md)."""

from __future__ import annotations

import os
from pathlib import Path

from conftest import assert_golden, golden_names

from wgt import render, rules, store


def lines(xs):
    return "".join(x + "\n" for x in xs)


def test_wg0_conf(server):
    got = render.render_wg_conf(server.cfg, server.snap, server.pub_if, store.psk_reader(server.paths))
    assert_golden(server.case, "wg0.conf", server.norm(got))


def test_warp_conf(server):
    profile = Path(server.paths.wg("wgcf", "wgcf-profile.conf")).read_text()
    got = render.render_warp_conf(server.cfg, profile, has_ipv6=True, resolve4=lambda h: None)
    assert_golden(server.case, "warp.conf", got)


def test_hooks(server):
    for direction in ("up", "down"):
        got = lines(rules.hook_cmds(server.cfg, server.snap, server.pub_if, up=direction == "up"))
        assert_golden(server.case, f"hooks-{direction}.txt", server.norm(got))


def test_ipt_rules(server):
    got = lines("|".join(r) for r in rules.ipt_rules(server.cfg, server.pub_if, server.snap.exits))
    assert_golden(server.case, "ipt-rules.txt", got)


def test_warp_switch(server):
    assert_golden(server.case, "warp-on.txt", lines(rules.warp_rule_cmds(server.cfg, True)))
    assert_golden(server.case, "warp-off.txt", lines(rules.warp_rule_cmds(server.cfg, False)))


def test_dropin(server):
    assert_golden(server.case, "dropin.conf", render.render_dropin(server.cfg))


def test_exits(server):
    for e in server.snap.exits:
        raw = Path(server.paths.wg("exits", f"{e.code}.proton.conf")).read_text()
        assert_golden(server.case, f"px-{e.code}.conf", render.render_jump_conf(raw, e))
        profile = Path(server.paths.wg("exits", e.code, "wgcf-profile.conf")).read_text()
        wx = render.render_warp_conf(server.cfg, profile, has_ipv6=True, resolve4=lambda h: None,
                                     table=rules.exit_wtable(e), mark=rules.exit_mark(e))
        assert_golden(server.case, f"wx-{e.code}.conf", wx)
        for direction in ("up", "down"):
            got = lines(rules.exit_hook_cmds(server.cfg, server.snap, e, up=direction == "up"))
            assert_golden(server.case, f"exit-{e.code}-{direction}.txt", got)
        got = lines("|".join(r) for r in rules.exit_ipt_rules(server.cfg, e))
        assert_golden(server.case, f"exit-{e.code}-ipt.txt", got)


def test_client_conf(server):
    name = next(n for n in golden_names(server.case) if n.startswith("client-"))[len("client-"):-len(".conf")]
    c = server.snap.client(name)
    got = render.render_client_conf(server.cfg, server.snap, name, c.ip, "CLIENT-PRIVATE-KEY",
                                    "SERVER-PUBLIC-KEY", "CLIENT-PSK", "ENDPOINT", "0.0.0.0/0,::/0")
    assert_golden(server.case, f"client-{name}.conf", got)


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
