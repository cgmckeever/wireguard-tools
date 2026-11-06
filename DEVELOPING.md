# Developing wireguard-tools

How the code is laid out, the conventions it keeps, and how it is tested.
README.md covers using it; [Updating servers](README.md#updating-servers)
covers rolling a change out.

## Layout

```
wgt/                      the package (Python 3.8+, standard library only)
  cli.py                  wgt: usage, dispatch, log, the hidden `debug` render
  ctx.py                  the command line (Args) and what every command gets (Ctx)
  ctl.py                  wgt-ctl (ssh/scp to the servers)
  const.py, errors.py     paths (Paths, one helper per file) and constants; WgtError
  config.py               settings: flag > env > saved > default, validation
  store.py                the server files: state, clients.db, exits.db (jumps), disabled, backup record; config names
  net.py, host.py         address maths; host facts and preflight
  rules.py                routing, NAT and firewall lines (wg0 PostUp/PostDown, WARP switch, jumps, per-client WARP)
  render.py               wg0.conf, warp.conf, px/wx, client configs, templates
  system.py               Runner (commands, dry-run, journal), FakeRunner, Files (writes with backups),
                          marker files
  live.py                 read-only queries: handshakes, rules, tables, units, Cloudflare's trace
  ops.py                  shared operations: lock, confirm, setup checks, state, endpoint, wg0.conf,
                          peers, the client/exit databases, subcommand dispatch
  warp.py, units.py       wgcf and WARP profiles; systemd units, watchdog timers, ufw
  server.py               setup, rollback (setup's inverse)
  backup.py               backup, restore (archive checks)
  clients.py              client commands, the clients table (and QR, share)
  devices.py              static device addresses and groups: numbers, `wgt device`, the watchdog's rules
  fleet.py                local/devices: wgt-ctl's record of each device's number (pure: no ssh, no files)
  exits.py                `wgt jump` (alias `exit`): bringing a jump up/down, probing it
  checks.py               test, status, summary
  results.py              PASS/FAIL counts shared by `test` and `jump test`
  watchdog.py             the watchdogs (the jump watchdog's rule is the pure `decide`)
  ui.py                   ages, prompts, numbered menus, the clients table's columns
bin/wgt, bin/wgt-ctl      launchers (put the checkout on sys.path and call main)
templates/                client config and systemd unit templates
tests/                    pytest: unit, golden files, FakeRunner commands, setup -> rollback
tools/                    render-diff.sh (before a deploy), trial.sh (full cycle on a throwaway
                          server), golden-bash.sh (how the golden files were first made)
test.sh, pyproject.toml   tests and lint (sets up .venv on first use); lint settings
```

## Naming

User-facing, a second hop is a **jump** (`wgt jump`, `--exit <code>`); the
code still says exit (`exits.py`, `store.Exit`, `exits.db`), and files and
interfaces keep their names (`exits/<code>.proton.conf`, `px-`/`wx-<code>`),
so running servers need no migration. Nothing in the code is Proton-specific:
any single-peer WireGuard config with an IPv4 address is a jump.

A **client** is a device; each of its **configs** is one clients.db entry
(one peer, key and address) named `[warp.][<jump>.]<device>`
(`store.split_name` / `config_name`). The name is the only record of a
config's exit and WARP: no switch, no side file. Code still says "client" for
an entry (`store.Client`, `clients.py`, `clients.db`). A device's static number
is per device, not per config, so it lives in `devices.db`.

## Conventions

- Errors raise one `WgtError`, which prints `ERROR: ...` and exits 1. A
  failure part-way through setup removes the wg0 rules it had applied.
- Every change goes through `Runner` or `Files`, so it is dry-run aware and
  backed up first. Keys never reach a log line, a command line or a diff:
  diffs are redacted, and keys are passed through files or stdin. Two
  deliberate exceptions write directly: wg-tools.state and the server keys
  (`system.write_private`: no change copy on every write, only an explicit one
  before `--new-server-keys` replaces them; rollback reads the state rather
  than restoring it), and marker files in /run (`system.write_marker`).
- Database writes go through `ops` (`save_client`, `remove_client`,
  `set_disabled`, `save_exit`, `state_set`...), which reload `ctx.snap`:
  values read from the snapshot before such a call are stale after it.
- Things a test can't fake go through a seam: commands through the Runner
  (wgcf too, via `run_logged`), downloads through `install_wgcf(fetch=)`,
  "now" through `ctx.clock`.
- Type hints throughout (`ctx: Ctx`, imported under `TYPE_CHECKING`);
  `dataclasses` for clients, exits and settings. `./test.sh lint` (ruff,
  mypy) stays clean. Module-level type aliases use `typing.List`/`Optional`:
  they are evaluated at runtime, where 3.8 has no `list[...]` or `X | Y`.
- Output goes through `Out`: `==> ` steps, 4-space info lines, `Out.line` for
  tables, WARN/ERROR on stderr. Only raw payloads (a config, a QR code, the
  debug render, the help text) are written straight to stdout.
- Prompts: standalone questions are indented 4 spaces (`ui.yes`, `ui.ask`), a
  menu's prompt 2 (as its title). Steps start with a verb and carry an
  accurate undo hint.
- Commands refuse leftover words (`ops.no_more_words`). Errors: `usage: ...`
  for missing arguments; `unknown <x> command: ... (see: wgt help)` for a
  wrong one (`ops.dispatch` builds it from the command table).
- Paths come from `Paths` helpers (const.py), never literals, so tests can
  move every one of them into a temp dir.
- Docstrings and comments say why, not what.
- `wgt-ctl`'s own steps (`jump-add`, `clients`) run only `wgt` commands every
  server version knows, since servers may not have pulled yet.

## Tests

- **Golden files.** `tests/golden/<case>/` holds the configs, hooks and rules
  rendered from each fixture server in `tests/fixtures/<case>/wg`. They were
  first made by the bash version (`tools/golden-bash.sh`, from git tag
  `bash-final`), so they pin byte-for-byte compatibility with what servers
  already run. For a deliberate rendering change, regenerate them and review
  the diff before committing:

  ```bash
  UPDATE_GOLDEN=1 ./test.sh -k golden && git diff tests/golden
  ```

- **Fixture keys are fake** and look it (`FAKE0001xxx…FAKE=`), so nothing in
  the repo can be mistaken for a real key. New fixtures should do the same.
- **Commands on a fake server.** `make_ctx` (tests/conftest.py) builds the Ctx
  a command gets, on a copy of a fixture server in a temp dir, with every
  system path moved there and a `FakeRunner` that records commands instead of
  running them. `tests/test_server.py` runs a whole `setup` then
  `rollback --purge` this way and checks nothing is left behind.
- **Live checks.** `tools/render-diff.sh` (read-only, every server) before a
  deploy; `tools/trial.sh` on a throwaway server for the full cycle.
- CI (`.github/workflows/tests.yml`) runs the tests on Python 3.8 and 3.12,
  and the lint.
