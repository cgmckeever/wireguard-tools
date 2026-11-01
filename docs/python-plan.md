# Python port: plan

**Status:** steps 1-7 and the clean-up are done on branch `python`; the bash
version is removed from the branch (git tag `bash-final` keeps it). boba runs
the Python version; ig88 and dengar switch after the merge. This document is
the plan as agreed; README.md describes the tool as it is.

Branch `python`. The goal is the same tool, with the same behaviour and the same
files on the servers, rewritten in Python so it can be unit-tested and so the
bash pitfalls go away. Bash stays the live tool on each server until the
Python version has proved it produces identical results there.

## Decisions

| | |
|---|---|
| Server command | `wgt`, with noun-verb subcommands |
| Mac command | `wgt-ctl`; the server is the first word: `wgt-ctl boba client add phone --exit par-fr` |
| Menus | numbered everywhere (lists and actions); Enter exits |
| Watchdogs | the systemd timers run `wgt watchdog warp` and `wgt watchdog exits` |
| Python | 3.8+, standard library only (servers need nothing installed) |
| Server files | unchanged formats: `wg-tools.state`, `clients.db`, `exits.db`, `clients.disabled`, the backup record, configs, units |
| Proof | byte-identical rendered configs from each live server's state; full trial on bossk |
| Tests | pytest on your machine (`./test.sh`); GitHub Actions on every push |
| Scope | same features and safety first (dry-run, backups, rollback, keys never printed); improvements after parity |

## Commands

### `wgt` (on a server, as root)

```
wgt setup [--name N --subnet ... --port ...]   install or update everything
wgt status | test | log [N] [--no-follow]     (log follows like tail -f)
wgt warp [on|off]
wgt backup [dir] | restore <archive> | rollback [--purge]

wgt client list [--tsv]
wgt client add <name> [--exit par-fr[,...]] [--allowed-ips ..] [--qr|--no-qr]
wgt client show <name> [--qr] | share <name>
wgt client rotate <name> | rename <old> <new> | disable <name> | enable <name> | remove <name>

wgt exit list | add <code> <file|-> [--label "Paris, France"] | remove <code> | test

wgt watchdog warp | exits        (run by the timers)
```

Global flags as today: `--dry-run`, `--yes`, `--force`. A missing name opens
a numbered picker.

### `wgt-ctl` (on your machine)

```
wgt-ctl                          servers: branch@commit or unreachable, exits staged
wgt-ctl init                     new server (asks address and name) or adopt one
wgt-ctl clients                  every client on every server
wgt-ctl <server|all> <wgt command ...>    e.g. wgt-ctl boba client add phone --exit par-fr
wgt-ctl <server|all> pull | backup        (backup fetched to local/backups/<server>/)
wgt-ctl <server> exit-add [n|file|all] | ssh | branch <b> | forget [--yes]
```

The first word is a server from `local/servers`, `all`, or one of `init` and
`clients`, so servers can't use those three names. With no server, commands
that need one show the numbered server list. `local/` keeps its layout:
`servers`, `exits/<server>/*.conf`, `exits/details.tsv`, `backups/<server>/`.

## Layout

```
wgt/                      the package (stdlib only)
  cli.py                  wgt: usage, dispatch, log, debug render
  ctx.py                  the command line (Args) and what every command gets (Ctx)
  ctl.py                  wgt-ctl (ssh/scp to the servers)
  const.py, errors.py     paths (Paths, one helper per file) and constants; WgtError
  config.py               settings: flag > env > saved > default, validation
  store.py                the server files: state, clients.db, exits.db, disabled, backup record
  net.py, host.py         address maths; host facts and preflight
  rules.py                routing, NAT and firewall lines (wg0 PostUp/PostDown, WARP switch, exits)
  render.py               wg0.conf, warp.conf, px/wx, client configs, templates
  system.py               Runner (commands, dry-run, journal), FakeRunner, Files (writes with backups)
  live.py                 read-only queries: handshakes, rules, tables, units, Cloudflare's trace
  ops.py                  shared operations: lock, confirm, state, endpoint, wg0.conf, peers,
                          the client/exit databases, subcommand dispatch
  warp.py, units.py       wgcf and WARP profiles; systemd units, watchdog timers, ufw
  server.py               setup, warp on|off, rollback (setup's inverse)
  backup.py               backup, restore (archive checks)
  clients.py              client commands, the clients table (and QR, share)
  exits.py                exit commands
  checks.py               test, status
  results.py              PASS/FAIL counts shared by `test` and `exit test`
  watchdog.py             both watchdogs
  ui.py                   ages, numbered menus, prompts
bin/wgt, bin/wgt-ctl      launchers (put the checkout on sys.path and call main)
templates/                client config and systemd unit templates
tests/                    pytest: unit, golden files, FakeRunner
tools/                    parity.sh + parity-remote.sh (live render comparison),
                          golden-bash.sh (goldens from bash-final), trial.sh (full cycle)
test.sh                   runs the tests (sets up .venv on first use)
```

## How each step is proved

1. **Unit tests** for the pure parts:
   - name and code rules, subnet maths and renumbering;
   - which files `remove` deletes (the `par-fr.laptop` case);
   - db read/write round-trips;
   - menu input parsing.
2. **Golden files.** Rendered `wg0.conf`, `warp.conf`, `px-*`/`wx-*` and client
   configs from fixture states covering:
   - WARP on and off;
   - IPv6 on and off;
   - disabled clients;
   - one or two exit cities;
   - a renamed server.

   The expected files come from the bash version, so they define parity.
3. **Command parity with a fake runner.** For `setup`, `exit add`, `client
   add`, `remove` and `rollback`, the recorded commands are compared with what
   the bash version prints under `--dry-run`. Tests also check that rollback
   undoes exactly what setup did.
4. **Live render parity, read-only, on dengar, ig88 and boba.** `tools/parity.sh`
   renders every config and rule list with both versions from the server's
   real state and diffs them, with keys redacted. This is the same check used
   during the review, made repeatable.
5. **Trial on bossk.** The full cycle, then a diff against bossk's state before
   the trial:
   - setup;
   - clients;
   - an exit city;
   - on-demand up and down;
   - fail-closed;
   - watchdog recovery;
   - backup and restore;
   - `rollback --purge`.

## Order

1. **Skeleton:** the package, `wgt` and `wgt-ctl` argument parsing, Runner and
   fake runner, pytest and the GitHub Actions workflow.
2. **Pure core:** config, store, net, rules and render, with unit and golden
   tests. Live render parity on all three servers.
3. **Read-only commands:**
   - `status`, `test`, `log`;
   - `client list` (and `--tsv`), `exit list`.

   Output is compared with bash on each server.
4. **Client commands:** add, show, share, rotate, rename, disable/enable,
   remove, plus the numbered menus.
5. **Exits and the watchdogs:** `exit add/remove/test`, `wgt watchdog`.
6. **Server lifecycle:** setup, `warp on|off`, backup, restore and rollback.
   Command parity, then the bossk trial.
7. **`wgt-ctl`:** list, init/adopt, clients, pull/branch, exit-add,
   backup fetch, forget, and running `wgt` commands remotely.
8. **Switch over, one server at a time (bossk, boba, ig88, dengar):**
   - `wgt setup` links `wgt` and points the watchdog units at
     `wgt watchdog`;
   - `wgt test` must pass;
   - clients stay connected throughout, because `wg0.conf` is identical.
9. **Clean up:**
   - the README is rewritten for `wgt` and `wgt-ctl`;
   - `wg-setup.sh`, `wg-client.sh` and `scripts/servers.sh` become one-line
     wrappers pointing at the new commands for a while, then are removed;
   - the branch merges to `main`.

Bash stays the live tool on a server until step 8 for that server. Nothing
that ships before then changes a running server.

## Conventions

- Errors raise one `WgtError`, which prints `ERROR: ...` and exits 1. A
  failure part-way through setup removes the wg0 rules it had applied, as the
  ERR trap does today.
- Every change goes through `Runner` or `Files.write`, so it is dry-run aware
  and backed up first. Keys never reach a log line, a command line or a diff:
  diffs are redacted, and keys are passed through files or stdin.
  Two deliberate exceptions write directly: wg-tools.state and the server
  keys (`system.write_private`: no change copies of keys, and rollback
  reads the state rather than restoring it), and the watchdogs' marker
  files in /run.
- Things a test can't fake go through a seam: commands through the Runner
  (wgcf too, via `run_logged`), downloads through `install_wgcf(fetch=)`,
  "now" through `ctx.clock`. tests/test_server.py runs a whole setup and
  `rollback --purge` on the FakeRunner.
- Type hints throughout (`ctx: Ctx`, imported under `TYPE_CHECKING`);
  `dataclasses` for clients, exits and settings. ruff and mypy stay clean.
- Output goes through `Out`: `==> ` steps, 4-space info lines, `Out.line` for
  tables, WARN/ERROR on stderr. Only raw payloads (a config, a QR code, the
  debug render, the help text) are written straight to stdout.
- Prompts: standalone questions are indented 4 spaces, a menu's prompt 2 (as
  its title). Steps start with a verb and carry an accurate undo hint.
- Errors: `usage: ...` for missing arguments; `unknown <x> command: ...
  (see: wgt help)` for a wrong one.
- Paths come from `Paths` helpers (const.py), never literals, so tests can
  move every one of them into a temp dir.
- Module and function docstrings say why, not what, matching the current
  comments.
