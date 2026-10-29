# wireguard-tools

Turn a bare Debian 11+ / Ubuntu 20.04+ server (amd64 or arm64) into a WireGuard
VPN server, and manage its clients. [Cloudflare WARP](https://1.1.1.1/) is
installed alongside and works as a switch:

| `wg-setup.sh warp …` | Clients' traffic leaves via | Sites see | Use it for |
|------|----------------------------|-----------|------------|
| `on` (default) | Cloudflare WARP, tunnelled from the server | a Cloudflare IP | hiding the VPS's IP, getting Cloudflare's network |
| `off` | the server's own public interface | the VPS's IP | a plain, fast WireGuard VPN |

Switching is live: clients stay connected, only where their traffic exits changes.

Two commands, both run as root on the server:

- **`wg-setup.sh`** sets up the server (`setup`), switches WARP (`warp on|off`),
  plus `test`, `status`, `backup`, `restore`, `rollback`.
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

```bash
./wg-setup.sh setup --name dengar              # typical: just name it
./wg-setup.sh setup --name dengar --dry-run    # see exactly what it would change first
```

### Flags

All optional. Settings are saved in `/etc/wireguard/wg-tools.state`, so
re-runs and `wg-client.sh` reuse them; pass a flag again to change it. Each can
also be set as an environment variable (flag > env > saved > default).

| Flag | Env | Default | What it sets |
|------|-----|---------|--------------|
| `--name <name>` | `SERVER_NAME` | short hostname | server name; client configs are `<name>.<client>.conf` |
| `--port <port>` | `WG_PORT` | `51820` | UDP port clients connect to |
| `--endpoint <host-or-ip>` | `ENDPOINT` | detected public IPv4 | address written into client configs (a hostname lets you move servers without re-sending configs) |
| `--dns <ip>` | `WG_DNS` | `1.1.1.1` | DNS server given to clients |
| `--allowed-ips <cidrs>` | `CLIENT_ALLOWED_IPS` | `0.0.0.0/0, ::/0` (all traffic) | default for new clients (per client on `add`/`rotate`); `"10.8.0.0/24, fd…::/64"` for VPN-network only |
| `--subnet <cidr>` | `WG_SUBNET` | `10.8.0.0/24` | clients' IPv4 network (server is `.1`) |
| `--subnet6 <cidr>` | `WG_SUBNET6` | random `fd…::/64` | clients' IPv6 network; `off` disables client IPv6 |
| `--mtu <bytes>` | `WG_MTU` | `1280` | tunnel MTU for server and clients |
| *Rarely needed:* | | | |
| `--warp-mtu <bytes>` | `WARP_MTU` | `1280` | MTU of the WARP tunnel |
| `--wg-if` / `--warp-if` | `WG_IF` / `WARP_IF` | `wg0` / `warp` | interface names |
| `--table <n>` | `RT_TABLE` | `100` | routing table used for WARP |
| | `WGCF_VERSION` | `latest` | wgcf release to install, e.g. `v2.2.22` |
| | `WGCF_SHA256` | | pin the wgcf binary's checksum |

Example: `./wg-setup.sh setup --name dengar --port 51821 --endpoint vpn.example.com`.

Behaviour flags (not saved):

| Flag | Works with | Does |
|------|-----------|------|
| `--dry-run` | everything | show what would change; change nothing |
| `--yes`, `-y` | everything | don't prompt |
| `--force` | `setup` | regenerate server keys, reinstall wgcf, regenerate the WARP profile, change the subnet with clients present, take over configs these tools didn't create |
| `--purge` | `rollback` | also delete keys, clients and the WARP account |
| `--qr` / `--no-qr` | `wg-client.sh add`, `rotate`, `show` | force or skip the QR code |

`help` on either command lists everything.

### What setup does

Setup runs in two stages:

1. **WARP tunnel:** installs `wgcf`, registers a free WARP account, brings up
   the `warp` interface and tests it. (Registration refused? See
   [Register WARP on another machine](#register-warp-on-another-machine).)
2. **WireGuard server:** creates `wg0` with server keys, NAT for both ways out
   (WARP and the public interface), forwarding and systemd units, with WARP on.

If WARP can't be set up on a first install, setup still finishes, with WARP
off; fix it, then `./wg-setup.sh warp on`.

**Open UDP 51820** (or your `--port`) in your VPS provider's firewall /
security group. The script opens it on the server itself (ufw or iptables) but
can't reach the provider's firewall.

To change a setting, re-run setup with the new flag, e.g.
`./wg-setup.sh setup --port 51821`. Changing `--name` renames the client
configs; re-send them.

### WARP on or off

```bash
./wg-setup.sh warp off   # clients exit via this server's IP
./wg-setup.sh warp on    # clients exit through WARP (tests WARP first)
./wg-setup.sh warp       # show the current setting
```

The setting is saved and survives reboots. Client configs are the same either
way (MTU 1280 for both), so nothing needs re-sending. With WARP on, a WARP
outage blocks clients rather than letting them leak out through the VPS's own
IP. With it off, clients' IPv6 goes out the server's own IPv6; a server
without public IPv6 gives clients IPv4 only while WARP is off.

## Manage clients

```bash
./wg-client.sh add laptop                            # full tunnel (default); prints a QR code
./wg-client.sh add printer --allowed-ips "10.8.0.0/24, fdXX:XXXX:XXXX::/64" # split tunnel: VPN network only
./wg-client.sh list                                  # address, last seen, traffic, allowed IPs
./wg-client.sh show laptop                           # config with keys redacted
./wg-client.sh show laptop --qr                      # QR code again (phones: WireGuard app → scan)
./wg-client.sh share laptop                          # send the config via magic-wormhole (one-time code)
./wg-client.sh rotate laptop                         # new keys, same address; old config stops working
./wg-client.sh disable laptop                        # cut it off now; keys, config and address kept
./wg-client.sh enable laptop                         # let it back in with the same config
./wg-client.sh remove laptop
```

Leave out the name on `rotate`, `remove`, `disable`, `enable`, `show` or
`share` to pick from a numbered list. Changes apply live; other clients stay
connected.

`disable` removes the client's peer from the running `wg0` and leaves it out of
`wg0.conf`, so it stays off across restarts; `list` shows it as `disabled`.
Nothing on the device changes: its tunnel just gets no handshake (with a
full-tunnel config, no internet until its tunnel is switched off). `enable`
puts it back; the same config works, nothing to re-send. Disabled clients are
listed in `/etc/wireguard/clients.disabled`.

Each client gets its own keypair, a preshared key and the next free address.
Its config is written to `/etc/wireguard/clients/<server>.<name>.conf` (e.g.
`dengar.laptop.conf`), so configs from different servers don't collide.

### Getting a config onto a device

| How | Command | Best for |
|-----|---------|----------|
| Magic Wormhole | `./wg-client.sh share laptop`; recipient runs `wormhole receive <code>` | sending it to someone else, no SSH needed |
| QR code | printed by `add`/`rotate` in a terminal (`--no-qr` skips it); again any time with `./wg-client.sh show laptop --qr` | phones (WireGuard app → scan) |
| scp | `scp root@<server>:/etc/wireguard/clients/dengar.laptop.conf .` | your own machines |

`share` prints a one-time code (like `7-guitar-revenge-tidy`) and waits up to 10
minutes. The file is encrypted end to end; a wrong guess at the code ends the
transfer. The recipient needs magic-wormhole (`brew install magic-wormhole`,
`apt install magic-wormhole`, `pipx install magic-wormhole`) and then imports
the `.conf` into their WireGuard app.

In iTerm2 or WezTerm the QR code is drawn as an inline image 32 columns wide
(set `QR_WIDTH`, e.g. `QR_WIDTH=24`; `QR_WIDTH=0` for the text version, which
other terminals and tmux always get). The image is streamed, never written to disk.

Both the QR code and the wormhole transfer carry the client's private key:
send them only to that client's owner.

## Check, back up, restore, undo

```bash
./wg-setup.sh test              # PASS/FAIL checks; non-zero exit on any FAIL
./wg-setup.sh status            # interfaces, handshakes, routes, rules
./wg-setup.sh backup            # /var/backups/wireguard-tools/wireguard-tools.<host>.<date>.tar.gz
./wg-setup.sh restore <archive> # set this server up from a backup
./wg-setup.sh rollback          # stop everything, remove all rules, restore original files
./wg-setup.sh rollback --purge  # ...and delete keys, clients and the WARP account
```

`test` checks: WARP is up and handshaking, traffic through `warp` shows
`warp=on` while the server's own traffic shows `warp=off`, the server's
default route never changed, `wg0` is listening, forwarding is on, the WARP
rule is there exactly once (WARP on) or absent (off), every NAT/firewall rule
exists exactly once, the services start at boot in the right order, and
whether any client has connected. With WARP off, WARP problems are warnings.

### Moving to a new server (or rebuilding)

```bash
# old server
./wg-setup.sh backup
scp root@old:/var/backups/wireguard-tools/wireguard-tools.*.tar.gz .   # or: wormhole send <archive>

# new server (after Install)
./wg-setup.sh restore wireguard-tools.<host>.<date>.tar.gz
```

`restore` brings over the server keys, every client (same keys, addresses and
allowed IPs), the settings (including WARP on or off) and the WARP account
(so it doesn't need to register again), then runs setup. It won't overwrite a server that's already set up
(`rollback` or `rollback --purge` first).

The new server records its own default route and public interface, and an IP
endpoint is re-detected. If the address changed, every client config's
`Endpoint` is updated and the clients to re-send are listed (`wg-client.sh
share` or `show --qr`); their keys don't change. This also happens whenever a
re-run changes `--endpoint` or `--port`. An endpoint set as a hostname
(`--endpoint vpn.example.com`) is kept, so pointing that DNS name at the new
server means clients need nothing.

Backups go in `/var/backups/wireguard-tools/` (mode 700, outside the checkout,
so re-cloning the tools keeps them; `rollback` restores originals from its
`files/`); `backup <dir>` writes elsewhere. Each `backup` also prunes old archives and file copies. A backup holds
private keys (it's created mode 600); keep it somewhere safe. Stop the old
server once the new one works: both would be using the same WARP device.

## How it works

**WARP.** `wgcf` registers a free WARP account and produces a WireGuard
profile, written as `/etc/wireguard/warp.conf` with `Table = off`, so the
server's own routing is never touched and SSH stays reachable. Table 100 holds
a default route through `warp` plus a blackhole fallback. `warp.conf` re-adds
the route through `warp` whenever it comes up, so restarting WARP on its own
doesn't cut clients off. `wg-quick@wg0` starts after `wg-quick@warp` (and
wants it, without requiring it).

**The switch.** NAT and forwarding rules exist for both ways out (`warp` and
the public interface); each only matches its own outgoing interface. WARP on
adds one policy rule per address family, `ip rule from <VPN subnet> lookup
100`, so client traffic goes to table 100: out through `warp`, or into the
blackhole if WARP is down (fail closed). WARP off removes the rule, and client
traffic follows the server's normal routes out the public interface.
`warp on|off` adds or removes the rule live and rewrites `wg0.conf` so it
sticks; `wg0` isn't restarted.

**WARP watchdog.** Cloudflare occasionally stops answering the WARP tunnel's
handshakes (seen after some reboots: packets go out, nothing comes back, and
with WARP on clients are blocked). A new source port fixes it within seconds.
`wg-tools-warp-watchdog.timer` checks every 30s; if `warp` has had no
handshake for 180s on two checks in a row, it moves `warp` to a new source
port (`wg set warp listen-port 0`, no restart) and logs it
(`journalctl -t wg-tools-warp-watchdog`). `status` shows the last time it acted.

**Always.** All routing/NAT/firewall rules live in `wg0.conf`'s
`PostUp`/`PostDown`, generated from one list so `PostDown` and `rollback` undo
exactly what `PostUp` did. Forward rules are inserted at the top so ufw/Docker
can't block them, and TCP MSS is clamped to avoid MTU stalls.

**IPv6.** Each client also gets an IPv6 address in a private `fd…::/64`
(picked at random once at setup and saved; `--subnet6` sets your own), with the
same host number as its IPv4 address (`10.8.0.5` → `fd…::5`). Its IPv6 is
NATed out the same way as IPv4: with WARP on through WARP's own IPv6, which
works even on an IPv4-only server, with the same table 100 route and blackhole
fallback; with WARP off out the public interface, if the server has IPv6
(otherwise clients get IPv4 only). `wg-setup.sh status` shows which. Clients'
`AllowedIPs` include `::/0` either way, so their IPv6 can't bypass the tunnel.
Turning IPv6 on for an existing server (re-run setup) adds the address to
every client config; re-send them. `--subnet6 off` disables it.

### Files

| Path | What |
|------|------|
| `/etc/wireguard/wg0.conf` | server interface + rules; `[Peer]`s rebuilt from `clients.db` |
| `/etc/wireguard/warp.conf` | WARP tunnel |
| `/etc/wireguard/wgcf/` | WARP account and profile |
| `/etc/wireguard/server.key`, `server.pub` | server keypair |
| `/etc/wireguard/clients.db` | `name address public-key allowed-ips`, no private keys |
| `/etc/wireguard/clients.disabled` | names of disabled clients |
| `/etc/wireguard/psk/<name>.psk` | per-client preshared keys |
| `/etc/wireguard/clients/<server>.<name>.conf` | client configs (the only copy of each client's private key) |
| `/etc/wireguard/wg-tools.state` | saved settings (including `WARP=on|off`), recorded default route |
| `/etc/sysctl.d/99-wg.conf` | `net.ipv4.ip_forward = 1` |
| `/etc/systemd/system/wg-quick@wg0.service.d/` | start `wg0` after `warp` |
| `/usr/local/sbin/wg-tools-warp-watchdog`, `/etc/systemd/system/wg-tools-warp-watchdog.{service,timer}` | WARP watchdog |

Secrets are mode 600 in 700 directories. Before any file is changed, a
timestamped copy goes to `/var/backups/wireguard-tools/files/<its path>` (e.g.
`files/etc/wireguard/clients.db.<date>.bak`); `rollback` restores the
originals. Each file keeps its original plus the 5 newest copies, and
`wg-setup.sh backup` keeps the 10 newest archives.

### Templates

These files are rendered from `templates/`, filling `{{PLACEHOLDER}}`s:

| Template | Becomes |
|----------|---------|
| `client.conf.tmpl` | each `/etc/wireguard/clients/<server>.<name>.conf` |
| `wg-quick-after-warp.conf.tmpl` | the systemd drop-in that starts `wg0` after `warp` |
| `warp-watchdog.sh.tmpl`, `warp-watchdog.service.tmpl`, `warp-watchdog.timer.tmpl` | the WARP watchdog script and its systemd units |

Edit them to change what every new client gets (e.g. `PersistentKeepalive`).
Changes apply to clients added or rotated afterwards. Every placeholder must
stay: a missing value stops the script before anything is written.
Your edits are local changes to this git checkout: commit them (or
`git stash` around `git pull`) so updates don't stop on a conflict.
`wg0.conf` and `warp.conf` are built in code, since their contents depend on
the WARP setting, the routing rules and the client list.

## Troubleshooting

- **Clients can't connect:** the provider firewall usually still blocks UDP 51820.
- **`wgcf register` fails:** WARP may refuse registrations from some server IPs.
  Retry later, or register on another machine and copy the account over
  ([below](#register-warp-on-another-machine)). If the WARP tests still fail
  (no handshake), the provider is blocking WARP's traffic: `./wg-setup.sh warp off`.
- **Some sites hang with WARP on:** lower `--mtu` (e.g. 1200), re-run `./wg-setup.sh setup`, then `rotate` the clients so they get the new MTU.
- **Anything else:** `./wg-setup.sh status` and `./wg-setup.sh test`.

### Register WARP on another machine

If setup reports `wgcf register failed` (it then finishes with WARP off),
register the WARP account somewhere WARP accepts (your Mac: `brew install
wgcf`) and copy it over. Registration happens once, and is skipped when the
account file exists. The failed run already created `/etc/wireguard/wgcf/` for it.

```bash
# on your Mac
mkdir -p ~/wgcf-vps && cd ~/wgcf-vps
wgcf register --accept-tos     # a new WARP device, separate from your Mac's WARP app
wgcf generate                  # uses the API too, so do it here as well
scp wgcf-account.toml wgcf-profile.conf root@vps:/etc/wireguard/wgcf/
ssh root@vps 'chmod 600 /etc/wireguard/wgcf/*'
cd ~ && rm -rf ~/wgcf-vps      # local copies hold the device's private key
```

Then run `./wg-setup.sh warp on` on the server; it uses the copied account,
tests WARP and switches clients to it. Use these files on that server only: they are one WARP device.

## Upgrading from the old scripts

The earlier `scripts/` versions (`setup.sh`, `client-setup.sh`, …) are in git
history. The new tools won't overwrite a `wg0.conf` they didn't create
(`--force` overrides). To move a server over: back up `/etc/wireguard`, run
`wg-setup.sh` (with `--force`), then re-add clients with `wg-client.sh add`.
