# CLAUDE.md — working on wireguard-tools

`wgt` runs on Linux servers (from a clone at `/opt/wireguard-tools`); `wgt-ctl`
drives them from this machine. README.md says what it does, DEVELOPING.md how
the code is laid out and the conventions it keeps. This file is what the code
promises to things outside it, and how to change it without breaking them.

## Contracts

**The shared store with wireguard-tunnels**: `$WG_TUNNELS`, else `~/wg-tunnels`
(`wgt/ctl/tunnels.py`, `jumpfiles.py`; pinned by `tests/test_tunnels_store.py`).
The other side's rules are in that repo's `wgtun/store.py` (`sweep_owned`).

- **Writes** `devices/<device>/config/<server>.<config>.conf`, the server's file
  as is, starting `# wireguard-tools client: <config> on <server>` (`<server>`
  is the server's own SERVER_NAME from that line). Folders 0700, files 0600.
  Replaced text and copies the server no longer has become
  `<file>.<YYYYmmdd-HHMMSS>.bak`. A file of that name that isn't a copy is
  refused, never overwritten.
- **Reads** `servers/<server>/carriers/*.conf` (jump-add). It also makes that
  folder (init, sync: how wireguard-tunnels knows the server exists), deletes
  it when empty (remove), and writes a `# jump: <code> <label>` line into a
  file on its first push. nord.sh writes the same line, with the same code rule
  (`store.valid_exit_code`).
- **Never touches** `config/warped.*` (the sweep's: they keep the carrier's key
  and header line, so only the name tells them apart; `local_files` skips them)
  or `config/warp.<device>.conf` (the sweep's WARP alone). Nor anything else in
  the store.
- **Matching** a local file to a server's config: the same private key (rename
  keeps keys), else the same header line. A matched file keeps its name.
  Retiring needs the server's complete list, a header naming this server, and a
  key no live config has.
- **Passed straight through.** The WireGuard app, `tunnel.sh` and `singbox.sh`
  run a config's settings as written, PersistentKeepalive included: what this
  repo writes is what every device runs. They also read two header lines:
  `# wireguard-tools client:` (tunnel.sh extract leaves those files alone) and
  `# device-network:` (the sweep routes it through a carrier).

**The servers** (`local/servers`, untracked). dengar, ig88 and boba are live:
people's traffic runs through them. bossk is the throwaway for anything
destructive (`tools/trial.sh`, setup/rollback, restore, `--new-server-keys`).
Live servers are never used for those. `wgt-ctl all` means every listed
server, bossk too.

**What's already out there**, which a change must keep reading:

- Client configs (`templates/client.conf.tmpl`): the header lines above, then
  `Key = value` lines, which setup rewrites in place on existing configs.
- The server's files and their formats (the list is at the top of
  `wgt/store.py`), and names running servers have: `exits/<code>.proton.conf`,
  `px-`/`wx-<code>`, `exits.db`, `WARP=` in the state file for setup done,
  pre-`NAMES` archives (`restore --old-names`).
- Backups: `wireguard-tools.<host>.<stamp>.tar.gz`, absolute paths, with
  `wg-tools.state` inside (`backup.check_archive`). `wgt-ctl backup` and
  `move` fetch the newest by that name (`rebuild.fetch_backup`).
- `wg0.conf`, hooks and rules, pinned byte for byte by `tests/golden/` (a
  change restarts wg0 at each server's next setup).
- `wgt-ctl` assumes every server runs a recent `wgt`: pull everywhere first.

## Rules

1. **One home per rule.** Find the module that owns it (DEVELOPING.md's
   layout) and call it. Extract on the second copy, not the sixth.
2. **Change a rule everywhere it's read.** Before changing behavior, grep every
   reader and writer of the same files, formats or server state, in both repos
   when the store is involved, and check they still agree.
3. **Small commits, one concern each.**
4. **Verify, don't assume.** `./test.sh all` (the pre-commit hook runs it;
   `./test.sh hooks` turns it on), plus FakeRunner, fakehost and `--dry-run`
   where they apply. Say what tests can't cover: real `ip`/`iptables`, timing,
   signals and systemd need bossk.
5. **Live-system caution.** Never run `wgt-ctl` or `wgt` against a real server
   without asking first, read-only ones (`tools/render-diff.sh`, `summary`)
   included.
6. **Constraints.** Stdlib only; Python 3.8 on servers (CI runs it). Private
   keys never on a command line or in a log: files or stdin.
