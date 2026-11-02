# Python port: plan

> **Historical.** This was the plan for porting the bash tools to Python. It
> was completed on 2026-11-01: every server runs `wgt` from `main`, and the
> bash version is git tag `bash-final`. README.md describes the tool as it is
> now, and DEVELOPING.md the layout, conventions and tests.

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
wgt-ctl <server> exit-add [n|all] | ssh | branch <b> | forget [--yes]
```

The first word is a server from `local/servers`, `all`, or one of `init` and
`clients`, so servers can't use those three names. With no server, commands
that need one show the numbered server list. `local/` keeps its layout:
`servers`, `exits/<server>/*.conf`, `exits/details.tsv`, `backups/<server>/`.

## Layout

See DEVELOPING.md (kept current there).

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

See DEVELOPING.md (kept current there).
