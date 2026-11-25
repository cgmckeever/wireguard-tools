"""The history file: every change in one line (when, what, who), kept by logrotate
for HISTORY_MONTHS, read back by `wgt history`, and carried by backup and restore."""

import gzip
import os
import tarfile
from pathlib import Path

import pytest

from wgt import backup, cli, render, system
from wgt.errors import WgtError


def test_line_says_when_what_and_who():
    ctx = system.history_context(["/opt/wireguard-tools/bin/wgt", "client", "add", "tablet"],
                                 {"SSH_CLIENT": "203.0.113.5 50122 22", "SUDO_USER": "pi"})
    assert ctx == "wgt client add tablet; ssh from 203.0.113.5; sudo pi"
    line = system.history_line("wg-tools", "wgt: Add config 'tablet'\nat 10.77.1.4", ctx, when=0)
    assert line == ("1970-01-01T00:00:00Z wg-tools: wgt: Add config 'tablet' at 10.77.1.4"
                    "  [wgt client add tablet; ssh from 203.0.113.5; sudo pi]\n")


def test_runner_appends_mode_600(tmp_path):
    r = system.Runner()
    r.history_file = str(tmp_path / "h.log")
    r.record("wg-tools", "wgt: one")
    r.record("wg-tools", "wgt: two")
    lines = Path(r.history_file).read_text().splitlines()
    assert [x.split(" ", 1)[1].split("  [")[0] for x in lines] == ["wg-tools: wgt: one", "wg-tools: wgt: two"]
    assert os.stat(r.history_file).st_mode & 0o777 == 0o600


def test_runner_never_raises_and_dry_run_writes_nothing(tmp_path):
    r = system.Runner()
    r.history_file = str(tmp_path / "missing-dir" / "h.log")
    r.record("wg-tools", "wgt: x")   # no such directory: swallowed
    r = system.Runner(dry_run=True)
    r.history_file = str(tmp_path / "h.log")
    r.record("wg-tools", "wgt: x")
    assert not os.path.exists(r.history_file)


def test_step_records(make_ctx):
    ctx = make_ctx()
    ctx.runner.step("Add config 'x'", "wgt client remove x")
    assert ctx.runner.history == ["wg-tools: wgt: Add config 'x'"]


def test_read_history_oldest_first(tmp_path):
    p = str(tmp_path / "wireguard-tools.log")
    with gzip.open(p + ".3.gz", "wt") as f:
        f.write("a\n")
    with gzip.open(p + ".2.gz", "wt") as f:
        f.write("b\n")
    Path(p + ".1").write_text("c\n")
    Path(p).write_text("d\ne\n")
    Path(p + ".bak").write_text("not history\n")
    assert system.read_history(p) == ["a", "b", "c", "d", "e"]


def test_history_command(make_ctx, capsys):
    ctx = make_ctx()
    Path(ctx.paths.history_file).write_text("".join(f"line {i}\n" for i in range(50)))
    cli.cmd_history(ctx, ["3"])
    assert capsys.readouterr().out.splitlines() == ["line 47", "line 48", "line 49"]
    ctx.args.all = True
    cli.cmd_history(ctx, [])
    assert len(capsys.readouterr().out.splitlines()) == 50
    with pytest.raises(WgtError):
        cli.cmd_history(ctx, ["x"])


def test_logrotate_rule():
    text = render.render_logrotate("/var/log/wireguard-tools.log", "12")
    assert "/var/log/wireguard-tools.log {" in text
    assert "    monthly\n    rotate 12\n" in text and "create 0600 root root" in text
    assert "    su root root\n" in text   # Ubuntu's /var/log is group-writable


@pytest.mark.parametrize("months,ok", [("12", True), ("1", True), ("120", True), ("0", False), ("x", False)])
def test_history_months_setting(make_ctx, months, ok):
    from wgt.config import resolve
    cfg = resolve({"HISTORY_MONTHS": months}, {}, {}, hostname="h")
    if ok:
        cfg.finalize(kernel_ipv6=True)
        assert months == cfg.HISTORY_MONTHS
    else:
        with pytest.raises(WgtError, match="history months"):
            cfg.finalize(kernel_ipv6=True)


def test_restore_puts_the_archives_history_first(make_ctx, tmp_path):
    ctx = make_ctx()
    p = ctx.paths.history_file
    src = tmp_path / "src"
    src.mkdir()
    old, rot = src / "cur", src / "rot"
    old.write_text("old server 1\nold server 2\n")
    rot.write_bytes(gzip.compress(b"older month\n"))
    archive = str(tmp_path / "a.tgz")
    with tarfile.open(archive, "w:gz") as t:
        t.add(str(old), arcname=p.lstrip("/"))
        t.add(str(rot), arcname=(p + ".1.gz").lstrip("/"))
    Path(p).write_text("this server\n")
    members = [p, p + ".1.gz"]
    backup.restore_history(ctx, archive, members)
    assert Path(p).read_text() == "old server 1\nold server 2\nthis server\n"
    assert gzip.decompress(Path(p + ".1.gz").read_bytes()) == b"older month\n"
    assert os.stat(p).st_mode & 0o777 == 0o600
