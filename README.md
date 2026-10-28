# wireguard-tools

Turn a bare Debian 11+ / Ubuntu 20.04+ server (amd64 or arm64) into a WireGuard
VPN server, and manage its clients. Pick one of two modes:

| Mode | Clients' traffic leaves via | Sites see | Use it for |
|------|----------------------------|-----------|------------|
| `direct` | the server's own public interface | the VPS's IP | a plain, fast WireGuard VPN |
| `warp` | [Cloudflare WARP](https://1.1.1.1/), tunnelled from the server | a Cloudflare IP | hiding the VPS's IP, getting Cloudflare's network |

Two commands, both run as root on the server:

- **`wg-setup.sh`** sets up the server (`direct` or `warp`), plus `test`,
  `status`, `backup`, `restore`, `rollback`.
- **`wg-client.sh`** manages clients: `add`, `rotate`, `remove`, `list`, `show`, `share`.

Both are safe to re-run, support `--dry-run`, back up every file they change,
and never print private keys unless you ask for a QR code.

## Install

Nothing else is needed on the server; `wg-setup.sh` installs its own packages.

```bash
sudo -i   # everything below runs as root
apt-get update && apt-get install -y git
git clone https://github.com/cgmckeever/wireguard-tools.git /opt/wireguard-tools
cd /opt/wireguard-tools
```

Optional, to run them from anywhere:
`ln -s /opt/wireguard-tools/wg-setup.sh /opt/wireguard-tools/wg-client.sh /usr/local/sbin/`

Update later with `git -C /opt/wireguard-tools pull`.

## Set up the server

Run one of these. Add `--dry-run` first to see exactly what it will change.
Give the server a name with `--name` (e.g. `./wg-setup.sh warp --name dengar`);
it defaults to the short hostname and prefixes every client config file.

### Direct

```bash
./wg-setup.sh direct
```

Creates `wg0` with server keys, NAT out the server's public interface, IPv4
forwarding and its systemd unit.

### WARP

```bash
./wg-setup.sh warp
```

Runs in two stages:

1. **WARP tunnel:** installs `wgcf`, registers a free WARP account, brings up
   the `warp` interface and tests it. If a test fails it stops here, with
   nothing else changed. (Registration refused? See
   [Register WARP on another machine](#register-warp-on-another-machine).)
2. **WireGuard server:** asks to continue (`--yes` skips the question), then
   creates `wg0` routed out through `warp`. Answering no stops cleanly; run
   `./wg-setup.sh warp` again later and it picks up where it left off.

### Then, for either mode

**Open UDP 51820** (or your `--port`) in your VPS provider's firewall /
security group. The script opens it on the server itself (ufw or iptables) but
can't reach the provider's firewall.

To change a setting, re-run the same command with the new flag, e.g.
`./wg-setup.sh direct --port 51821`. The mode is saved; to switch, run
`./wg-setup.sh rollback`, then the other command. Existing clients keep working
after a switch. Changing `--name` renames the client configs; re-send them.

## Manage clients

```bash
./wg-client.sh add laptop                            # full tunnel (default)
./wg-client.sh add printer --allowed-ips "10.8.0.0/24, fdXX:XXXX:XXXX::/64" # split tunnel: VPN network only
./wg-client.sh list                                  # address, last seen, traffic, allowed IPs
./wg-client.sh show laptop                           # config with keys redacted
./wg-client.sh share laptop                          # send the config via magic-wormhole (one-time code)
./wg-client.sh rotate laptop                         # new keys, same address; old config stops working
./wg-client.sh remove laptop
```

Leave out the name on `rotate`, `remove`, `show` or `share` to pick from a
numbered list. Changes apply live; other clients stay connected.

Each client gets its own keypair, a preshared key and the next free address.
Its config is written to `/root/clients/<server>.<name>.conf` (e.g.
`dengar.laptop.conf`), so configs from different servers don't collide.

### Getting a config onto a device

| How | Command | Best for |
|-----|---------|----------|
| Magic Wormhole | `./wg-client.sh share laptop`; recipient runs `wormhole receive <code>` | sending it to someone else, no SSH needed |
| QR code | `./wg-client.sh show laptop --qr` | phones (WireGuard app → scan) |
| scp | `scp root@<server>:/root/clients/dengar.laptop.conf .` | your own machines |

`share` prints a one-time code (like `7-guitar-revenge-tidy`) and waits up to 10
minutes. The file is encrypted end to end; a wrong guess at the code ends the
transfer. The recipient needs magic-wormhole (`brew install magic-wormhole`,
`apt install magic-wormhole`, `pipx install magic-wormhole`) and then imports
the `.conf` into their WireGuard app.

Both the QR code and the wormhole transfer carry the client's private key:
send them only to that client's owner.

## Check, back up, restore, undo

```bash
./wg-setup.sh test              # PASS/FAIL checks; non-zero exit on any FAIL
./wg-setup.sh status            # interfaces, handshakes, routes, rules
./wg-setup.sh backup            # backup/wireguard-tools.<host>.<date>.tar.gz
./wg-setup.sh restore <archive> # set this server up from a backup
./wg-setup.sh rollback          # stop everything, remove all rules, restore original files
./wg-setup.sh rollback --purge  # ...and delete keys, clients and the WARP account
```

`test` checks, depending on the mode: WARP is up and handshaking, traffic
through `warp` shows `warp=on` while the server's own traffic shows `warp=off`,
the server's default route never changed, `wg0` is listening, forwarding is
on, every routing/NAT rule exists exactly once, the services start at boot in
the right order, and whether any client has connected.

### Moving to a new server (or rebuilding)

```bash
# old server
./wg-setup.sh backup
scp root@old:/opt/wireguard-tools/backup/wireguard-tools.*.tar.gz .   # or: wormhole send <archive>

# new server (after Install)
./wg-setup.sh restore wireguard-tools.<host>.<date>.tar.gz
```

`restore` brings over the server keys, every client (same keys, addresses and
allowed IPs), the settings and, in WARP mode, the WARP account (so it doesn't
need to register again). It then runs the same setup the backup came from
(`direct` or `warp`). It won't overwrite a server that's already set up
(`rollback` or `rollback --purge` first).

The new server records its own default route and public interface, and an IP
endpoint is re-detected. If the address changed, every client config's
`Endpoint` is updated and the clients to re-send are listed (`wg-client.sh
share` or `show --qr`); their keys don't change. This also happens whenever a
re-run changes `--endpoint` or `--port`. An endpoint set as a hostname
(`--endpoint vpn.example.com`) is kept, so pointing that DNS name at the new
server means clients need nothing.

Backups go in `backup/` inside the checkout (mode 700, ignored by git, so
`git pull` never touches them); `backup <dir>` writes elsewhere. A backup holds
private keys (it's created mode 600); keep it somewhere safe. In
WARP mode, stop the old server once the new one works: both would be using the
same WARP device.

## Options

Flags work on both commands. Settings chosen at setup are saved in
`/etc/wireguard/wg-tools.state`, so later runs and `wg-client.sh` use them
automatically. Precedence: flag > environment variable > saved > default.

| Flag | Env | Default | |
|------|-----|---------|---|
| `--name` | `SERVER_NAME` | short hostname | server name; client configs are `<name>.<client>.conf` |
| `--subnet` | `WG_SUBNET` | `10.8.0.0/24` | VPN network; server is `.1` |
| `--subnet6` | `WG_SUBNET6` | random `fd…::/64` | clients' IPv6 network; `off` disables IPv6 |
| `--port` | `WG_PORT` | `51820` | UDP listen port |
| `--mtu` | `WG_MTU` | `1420` direct, `1280` warp | tunnel MTU for server and clients |
| `--warp-mtu` | `WARP_MTU` | `1280` | |
| `--dns` | `WG_DNS` | `1.1.1.1` | DNS server given to clients |
| `--endpoint` | `ENDPOINT` | detected public IPv4 | server address written into client configs |
| `--allowed-ips` | `CLIENT_ALLOWED_IPS` | `0.0.0.0/0, ::/0` | default for new clients; per client on `add`/`rotate` |
| `--wg-if` / `--warp-if` | `WG_IF` / `WARP_IF` | `wg0` / `warp` | interface names |
| `--table` | `RT_TABLE` | `100` | routing table for WARP mode |
| | `WGCF_VERSION` | `latest` | e.g. `v2.2.22` |
| | `WGCF_SHA256` | | pin the wgcf binary's checksum |

Plus `--dry-run`, `--yes` (no prompts), `--force` (regenerate keys, reinstall
wgcf, take over configs these tools didn't create), `--purge`, `--qr`.
`help` on either command lists everything.

## How it works

**WARP mode.** `wgcf` registers a free WARP account and produces a WireGuard
profile, written as `/etc/wireguard/warp.conf` with `Table = off`, so the
server's own routing is never touched and SSH stays reachable. Only packets
*from* the VPN subnet are policy-routed (`ip rule` → table 100) into `warp`
and NATed there. Table 100 also has a blackhole fallback: if WARP drops,
clients lose internet instead of leaking out through the VPS's own IP.
`warp.conf` re-adds table 100's route through `warp` whenever it comes up,
so restarting WARP on its own doesn't cut clients off.
`wg-quick@wg0` starts after, and stops with, `wg-quick@warp`.

**Direct mode.** Clients are NATed out the public interface; no extra routing.

**Both.** All routing/NAT/firewall rules live in `wg0.conf`'s
`PostUp`/`PostDown`, generated from one list so `PostDown` and `rollback` undo
exactly what `PostUp` did. Forward rules are inserted at the top so ufw/Docker
can't block them, and TCP MSS is clamped to avoid MTU stalls.

**IPv6.** Each client also gets an IPv6 address in a private `fd…::/64`
(picked at random once at setup and saved; `--subnet6` sets your own), with the
same host number as its IPv4 address (`10.8.0.5` → `fd…::5`). Its IPv6 is
NATed out the same way as IPv4: in WARP mode through WARP's own IPv6, which
works even on an IPv4-only server, with the same table 100 route and blackhole
fallback; in direct mode out the public interface, if the server has IPv6
(otherwise clients get IPv4 only). `wg-setup.sh status` shows which. Clients'
`AllowedIPs` include `::/0` either way, so their IPv6 can't bypass the tunnel.
Turning IPv6 on for an existing server (re-run setup) adds the address to
every client config; re-send them. `--subnet6 off` disables it.

### Files

| Path | What |
|------|------|
| `/etc/wireguard/wg0.conf` | server interface + rules; `[Peer]`s rebuilt from `clients.db` |
| `/etc/wireguard/warp.conf` | WARP tunnel (warp mode) |
| `/etc/wireguard/wgcf/` | WARP account and profile |
| `/etc/wireguard/server.key`, `server.pub` | server keypair |
| `/etc/wireguard/clients.db` | `name address public-key allowed-ips`, no private keys |
| `/etc/wireguard/psk/<name>.psk` | per-client preshared keys |
| `/root/clients/<server>.<name>.conf` | client configs (the only copy of each client's private key) |
| `/etc/wireguard/wg-tools.state` | saved settings, recorded default route |
| `/etc/sysctl.d/99-wg.conf` | `net.ipv4.ip_forward = 1` |
| `/etc/systemd/system/wg-quick@wg0.service.d/` | start `wg0` after `warp` (warp mode) |

Secrets are mode 600 in 700 directories. Every change leaves a timestamped
`.bak`; `rollback` restores the originals.

### Templates

Two files are rendered from `templates/`, filling `{{PLACEHOLDER}}`s:

| Template | Becomes |
|----------|---------|
| `client.conf.tmpl` | each `/root/clients/<server>.<name>.conf` |
| `wg-quick-after-warp.conf.tmpl` | the systemd drop-in that starts `wg0` after `warp` (warp mode) |

Edit them to change what every new client gets (e.g. `PersistentKeepalive`).
Changes apply to clients added or rotated afterwards. Every placeholder must
stay: a missing value stops the script before anything is written.
Your edits are local changes to this git checkout: commit them (or
`git stash` around `git pull`) so updates don't stop on a conflict.
`wg0.conf` and `warp.conf` are built in code, since their contents depend on
the mode, the routing rules and the client list.

## Troubleshooting

- **Clients can't connect:** the provider firewall usually still blocks UDP 51820.
- **`wgcf register` fails:** WARP may refuse registrations from some server IPs.
  Retry later, or register on another machine and copy the account over
  ([below](#register-warp-on-another-machine)). If the WARP tests still fail
  (no handshake), the provider is blocking WARP's traffic: use `direct`.
- **Some sites hang in WARP mode:** lower `--mtu` (e.g. 1200), re-run `./wg-setup.sh warp`, then `rotate` the clients so they get the new MTU.
- **Anything else:** `./wg-setup.sh status` and `./wg-setup.sh test`.

### Register WARP on another machine

If `./wg-setup.sh warp` stops with `wgcf register failed`, register the WARP
account somewhere WARP accepts (your Mac: `brew install wgcf`) and copy it over.
Registration happens once, and `warp` skips it when the account file exists.
The failed run already created `/etc/wireguard/wgcf/` for it.

```bash
# on your Mac
mkdir -p ~/wgcf-vps && cd ~/wgcf-vps
wgcf register --accept-tos     # a new WARP device, separate from your Mac's WARP app
wgcf generate                  # uses the API too, so do it here as well
scp wgcf-account.toml wgcf-profile.conf root@vps:/etc/wireguard/wgcf/
ssh root@vps 'chmod 600 /etc/wireguard/wgcf/*'
cd ~ && rm -rf ~/wgcf-vps      # local copies hold the device's private key
```

Then re-run `./wg-setup.sh warp` on the server; it uses the copied account and
carries on to the tests and the WireGuard server. Use these files on that server only: they are one WARP device.

## Upgrading from the old scripts

The earlier `scripts/` versions (`setup.sh`, `client-setup.sh`, …) are in git
history. The new tools won't overwrite a `wg0.conf` they didn't create
(`--force` overrides). To move a server over: back up `/etc/wireguard`, run
`wg-setup.sh` (with `--force`), then re-add clients with `wg-client.sh add`.
