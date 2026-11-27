# CLAUDE.md — working on wireguard-tools

`wgt` runs on Linux servers (from a clone at `/opt/wireguard-tools`); `wgt-ctl`
drives them from this machine. README.md says what it does, DEVELOPING.md how
the code is laid out and the conventions it keeps. This file is what the code
promises to things outside it, and how to change it without breaking them.

## Contracts

**The shared store with wireguard-tunnels**: `$WG_TUNNELS` as written (no `~`
expansion, as there; absolute, or refused), else `~/wg-tunnels` (`wgt/ctl/home.py` has the
layout; `tunnels.py`, `jumpfiles.py`; pinned by `tests/test_tunnels_store.py`). It holds
everything private this machine keeps, and the checkout holds none of it (no
`.gitignore` hides any). The other side's rules are in that repo's `wgtun/store.py`
(`sweep_owned`).

- **Writes** `devices/<device>/config/<server>.<config>.conf`, the server's file
  as is, starting `# wireguard-tools client: <config> on <server>` (`<server>`
  is the server's own SERVER_NAME from that line). Folders 0700, files 0600.
  Replaced text and copies the server no longer has become
  `<file>.<YYYYmmdd-HHMMSS>.bak` (`<file>.<YYYYmmdd-HHMMSS>.<n>.bak` for the
  nth more within that second). A file of that name that isn't a copy is
  refused, never overwritten.
- **Its own, beside them:** `fleet/servers/<server>/login` (the server list:
  one `user@host [/checkout]` line; listed when present, in name order; nord.sh
  offers the same servers), `fleet/servers/<server>/backups/` (fetched
  archives), `fleet/device-list.json` (static numbers) and `fleet/pucks/<puck>/` (tpk-ctl.sh:
  `puck.conf`, `wifi.conf`, `gateways/<gateway>.conf`). wireguard-tunnels reads
  only the logins' presence.
- **Reads** `fleet/servers/<server>/carriers/*.conf` (jump-add). It also makes
  that folder (init, sync), deletes the server's folder when nothing is left in
  it (remove: forget drops the login, and backups or jump files keep it), and writes a `# jump: <code> <label>` line into a
  file on its first push. nord.sh writes the same line, with the same code rule
  (`store.valid_exit_code`; a new code also isn't `all`, `store.new_exit_code`,
  which nord.sh's `xxx-cc`/`nd-cc` never are). Each push also makes the folder 0700 and the file
  0600 (they hold private keys).
- **Never touches** `config/warped.*` (the sweep's: they keep the carrier's key
  and header line, so only the name tells them apart) or
  `config/warp.<device>.conf` (the sweep's WARP alone). `tunnels.sweep_owned`
  is the same rule as theirs, without case on both sides (the Mac's file system
  ignores it; pinned by the same examples in both repos' tests): those names
  are never matched, and a config that would land on one (a server named
  `warp`) is refused. Nor anything else in the store.
- **UTF-8.** wireguard-tunnels refuses a config that isn't. A copy is the
  server's file byte for byte, so the server's files are kept UTF-8: a jump
  label is made so where it comes in and where it's read (`store.one_line`),
  and `jump add` rewrites its configs' `# exit:` line. Delivery warns about any
  that still isn't, naming that `jump add`.
- **Matching** a local file to a server's config: the same private key (rename
  keeps keys), else the same header line. A matched file keeps its name.
  Retiring needs the server's complete list, a header naming this server, and a
  key no live config has.
- **Passed straight through.** The WireGuard app, `tunnel.sh` and `singbox.sh`
  run a config's settings as written, PersistentKeepalive included: what this
  repo writes is what every device runs. They also read two header lines:
  `# wireguard-tools client:` (tunnel.sh extract leaves those files alone) and
  `# device-network:` (the sweep routes it through a carrier).

**The servers** (`~/wg-tunnels/fleet/servers/<server>/login`). dengar, ig88 and boba are live:
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
- The history, `/var/log/wireguard-tools.log` (and logrotate's `.N`, `.N.gz`):
  one line per change, `<UTC time> <tag>: <what>  [<command>; <from>]`, oldest
  first. `wgt history` reads it back, `restore` joins an archive's ahead of the
  server's own, and people read it: add to a line, don't reshape it. A server
  set up before `HISTORY_MONTHS` has no such key saved: 12 applies.
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
7. **Test clients are named `test-<something>`** (`test-phone`, `test-tab`), on
   any server, by wgt, wgt-ctl, tunnel-puck or `tools/trial.sh`. Every config
   is delivered to `~/wg-tunnels/devices/<device>/config/`, where an unmarked
   test device looks like a real one and nobody knows whether to keep it.
   Real devices are named for what they are (`iphone`, `steve`), and only when
   the person asks for one.

   **Remove test configs properly when the test is done**, unless there's a
   reason to keep them: then say which ones and why, and remove them as soon
   as that reason is gone. Properly means everywhere they went:
   - on the server: `wgt-ctl <server> client remove <config>` (that also drops
     the device's number once it has no configs left);
   - on this machine: wgt-ctl renames the copy in `~/wg-tunnels` to `.bak`;
     delete those, and the device's folder once nothing else is in it;
   - what the test added for them: a jump, a tunnel-puck gateway (`tpk-ctl.sh
     <puck> gateway remove <gateway>`), QR images.

   A test that stops part-way still cleans up what it made before it stops.
