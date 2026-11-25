"""The log viewer: history --follow (tail -F of the history file) and --step for
history and log (walk back through it, entry by entry)."""

import os
import re
import threading
import time
from io import StringIO
from pathlib import Path

import pytest

from wgt import cli, logview
from wgt.errors import WgtError

LINES = [f"2026-11-25T00:00:{i:02d}Z wg-tools: wgt: change {i}" for i in range(10)]


def test_keys_walk_back_and_forward():
    assert logview.KEYS[b"k"] == logview.KEYS[b"\r"] == logview.KEYS[b"\x1b[A"] == "older"
    assert logview.KEYS[b"j"] == logview.KEYS[b"\x1b[B"] == "newer"
    assert logview.KEYS[b"q"] == logview.KEYS[b"\x1b"] == "quit"


def test_move_stays_inside():
    assert logview.move("older", 9, 10, 5) == 8
    assert logview.move("older", 0, 10, 5) == 0
    assert logview.move("newer", 9, 10, 5) == 9
    assert logview.move("page_older", 3, 10, 5) == 0
    assert logview.move("oldest", 7, 10, 5) == 0
    assert logview.move("newest", 2, 10, 5) == 9
    assert logview.move("", 4, 10, 5) == 4


def test_window_keeps_older_entries_in_view():
    assert logview.window(5, 4, 10) == 0           # all fit
    assert logview.window(100, 99, 10) == 90       # at the newest: the last screenful
    start = logview.window(100, 50, 9)
    assert start <= 50 < start + 9 and 50 - start > 9 // 2   # more older ones above it than newer below
    assert logview.window(100, 0, 10) == 0


def test_search_both_ways_case_ignored():
    assert logview.search(LINES, 9, "CHANGE 3", older=True) == 3
    assert logview.search(LINES, 3, "change 7", older=False) == 7
    assert logview.search(LINES, 3, "change 7", older=True) is None
    assert logview.search(LINES, 9, "change 9", older=True) is None   # not the current one


def test_screen_marks_the_current_entry_and_shows_it_whole():
    long = LINES[:9] + ["x " * 80]
    text = logview.screen(long, 9, "wgt history", 60, 20)
    rows = text.split("\r\n")
    assert sum("\x1b[7m" in r for r in rows) == 1
    assert "wgt history: 10 of 10" in text
    assert len([r for r in rows if r.startswith(("x x", "  x"))]) >= 2   # wrapped in full
    assert all(len(re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", r)) < 60 for r in rows)


def test_follow_prints_new_lines_and_reads_a_rotated_file_from_its_start(tmp_path):
    p = tmp_path / "h.log"
    p.write_text("old\n")
    out = StringIO()
    done = threading.Event()
    t = threading.Thread(target=logview.follow, args=(str(p), out, 0.02, done.is_set))
    t.start()
    try:
        time.sleep(0.1)
        with open(p, "a") as f:
            f.write("new 1\nnew ")
        time.sleep(0.1)
        with open(p, "a") as f:
            f.write("2\n")
        time.sleep(0.1)
        os.rename(p, str(p) + ".1")    # logrotate's move, then a fresh file
        p.write_text("after\n")
        time.sleep(0.15)
    finally:
        done.set()
        t.join(2)
    assert out.getvalue() == "new 1\nnew 2\nafter\n"


def test_history_follow_and_step_from_the_command(make_ctx, monkeypatch, capsys):
    ctx = make_ctx()
    Path(ctx.paths.history_file).write_text("".join(x + "\n" for x in LINES))
    followed, stepped = [], []
    monkeypatch.setattr(logview, "follow", lambda path: followed.append(path))
    monkeypatch.setattr(logview, "step", lambda lines, title: stepped.append((lines, title)))
    monkeypatch.setattr(logview, "can_step", lambda: True)
    ctx.args.follow = True
    cli.cmd_history(ctx, ["2"])
    assert capsys.readouterr().out.splitlines() == LINES[-2:] and followed == [ctx.paths.history_file]
    ctx.args.follow, ctx.args.step = False, True
    cli.cmd_history(ctx, [])
    assert stepped == [(LINES, "wgt history")]   # --step: all of it
    cli.cmd_history(ctx, ["3"])
    assert stepped[-1][0] == LINES[-3:]
    ctx.args.follow = True
    with pytest.raises(WgtError, match="not both"):
        cli.cmd_history(ctx, [])


def test_step_needs_a_terminal(make_ctx, monkeypatch):
    ctx = make_ctx()
    ctx.args.step = True
    monkeypatch.setattr(logview, "can_step", lambda: False)
    with pytest.raises(WgtError, match="terminal"):
        cli.cmd_history(ctx, [])
    with pytest.raises(WgtError, match="terminal"):
        cli.cmd_log(ctx, [])


def test_log_step_reads_the_journal_without_following(make_ctx, monkeypatch):
    ctx = make_ctx()
    ctx.args.step = True
    asked, stepped = [], []
    monkeypatch.setattr(logview, "can_step", lambda: True)
    monkeypatch.setattr(logview, "step", lambda lines, title: stepped.append(lines))
    monkeypatch.setattr(ctx.runner, "query", lambda cmd: asked.append(cmd) or "-- Boot x --\na\nb\n")
    cli.cmd_log(ctx, [])
    assert "-f" not in asked[0] and asked[0][asked[0].index("-n") + 1] == "1000"
    assert stepped == [["a", "b"]]


def test_wgt_ctl_all_refuses_step_and_follow(monkeypatch):
    from wgt import ctl
    from wgt.ctl import remote
    monkeypatch.setattr(remote, "listed", lambda: None)
    for args in (["--step"], ["-f"], ["--follow"]):
        with pytest.raises(WgtError, match="one server"):
            ctl._on_all("history", args)
    with pytest.raises(WgtError, match="one server"):
        ctl._on_all("log", ["--step"])


def test_client_list_watch_redraws_the_table(make_ctx, monkeypatch):
    from wgt import clients
    ctx = make_ctx()
    ctx.args.watch = True
    drawn = []
    monkeypatch.setattr(logview, "can_step", lambda: True)
    monkeypatch.setattr(logview, "watch", lambda draw, title: drawn.extend([draw(), draw(), title]))
    clients.cmd_list(ctx, [])
    first, second, title = drawn
    assert first == second and first.startswith("DEVICE") and "LAST HANDSHAKE" in first
    assert all(c.name in first for c in ctx.snap.clients) and "client list on" in title
    monkeypatch.setattr(logview, "can_step", lambda: False)
    with pytest.raises(WgtError, match="terminal"):
        clients.cmd_list(ctx, [])
