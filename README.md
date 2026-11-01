# wireguard-tools

Turn a bare Debian 11+ / Ubuntu 20.04+ server (amd64 or arm64) into a WireGuard
VPN server, and manage its clients. [Cloudflare WARP](https://1.1.1.1/) is
installed alongside and works as a switch:

| `wg-setup.sh warp …` | Clients' traffic leaves via | Sites see | Use it for |
|------|----------------------------|-----------|------------|
| `on` (default) | Cloudflare WARP, tunnelled from the server | a Cloudflare IP | hiding the VPS's IP, getting Cloudflare's network |
| `off` | the server's own public interface | the VPS's IP | a plain, fast WireGuard VPN |

Switching is live: clients stay connected, only where their traffic exits changes.

**Exit cities** add a third way out, chosen per client: through a Proton VPN
server in a city you pick, with WARP inside it, so sites see a Cloudflare IP in
that city (see [Exit cities](#exit-cities)).

Two commands, both run as root on the server:

- **`wg-setup.sh`** sets up the server (`setup`), switches WARP (`warp on|off`),
  plus `test`, `status`, `backup`, `restore`, `rollback`.
- **`wg-client.sh`** manages clients: `add`, `rotate`, `remove`, `disable`, `enable`,
  `list`, `show`, `share`.

And one for your own machine: **`scripts/servers.sh`** sets up new servers
over SSH and runs all of the above on the servers you manage
([Managing servers from your machine](#managing-servers-from-your-machine)).

Both are safe to re-run, support `--dry-run`, back up every file they change,
and never print private keys except inside a client's QR code (shown by `add`
and `rotate`, or `show --qr`).

## Install

Nothing else is needed on the server; `wg-setup.sh` installs its own packages
(WireGuard, iptables, curl, ping, qrencode, magic-wormhole).

```bash
sudo -i   # everything below runs as root
apt-get update && apt-get install -y git
git clone https://github.com/cgmckeever/wireguard-tools.git /opt/wireguard-tools
cd /opt/wireguard-tools
```

Optional, to run them from anywhere:
`ln -s /opt/wireguard-tools/wg-setup.sh /opt/wireguard-tools/wg-client.sh /usr/local/sbin/`

Update later with `git -C /opt/wireguard-tools pull`.

### Managing servers from your machine

`scripts/servers.sh` (Mac or Linux, plain bash) does the same from your own
machine over SSH, for every server it knows. The list is `local/servers`,
untracked like everything under `local/`:

```
dengar  root@192.3.63.161
boba    root@23.94.74.221      [checkout dir, default /opt/wireguard-tools]
```

```bash
scripts/servers.sh                       # the servers, with branch@commit (or unreachable)
scripts/servers.sh init                  # new server: asks address + name, installs, runs setup, adds it
scripts/servers.sh init --branch exit-cities --address 1.2.3.4 --name lando -- --port 51821
scripts/servers.sh pull all              # git pull everywhere
scripts/servers.sh branch boba exit-cities
scripts/servers.sh status boba           # also setup, warp, test, exit, log, rollback (wg-setup.sh)
scripts/servers.sh log boba -f           # follow its log
scripts/servers.sh client                # pick a server, then its client list and action menu
scripts/servers.sh client boba add phone # any wg-client.sh command
scripts/servers.sh clients               # every client on every server: exit, last seen, traffic
scripts/servers.sh backup all            # wg-setup.sh backup, fetched to local/backups/<name>/
scripts/servers.sh exit-add boba         # pick from local/exits/boba/*.conf (or: all) -> exit cities
scripts/servers.sh ssh boba
scripts/servers.sh forget boba           # drop from the list; the server is untouched
```

A server is a list number or name (or `all` for pull, status, test, backup);
leave it out to pick from the list. `init` logs in as root, shows the host
key for you to check against your provider's console on first contact, offers
`ssh-copy-id` if only a password works, then installs git, clones this repo's
`origin` and runs `wg-setup.sh setup --name <name>` with its usual prompts.
The server is added to the list only once setup finishes. A server already
running wireguard-tools is adopted under its saved name instead.

## Set up the server

```bash
./wg-setup.sh setup --name dengar              # typical: just name it
./wg-setup.sh setup --name dengar --dry-run    # see exactly what it would change first
```

The first setup asks for the client subnet, offering a random
`10.<1-10>.<1-10>.0/24` that doesn't overlap a route the server already has
(`Client subnet [10.4.5.0/24]:`; Enter takes it). `--subnet`, `--yes` or
`--dry-run` skip the question and use that random one.

Every run then shows the settings it will use and where each came from (flag,
env, entered, saved, default, detected, random), plus what changed since the last setup, or that
nothing did, and asks `Continue? [Y/n]` before changing anything. `--yes`
skips the per-step questions but not this one; it only carries on past it when
there's no terminal (a script). `--dry-run` shows it without asking.

If the server name (`--name`) differs from the machine's hostname, setup then
asks `Update hostname <current> to <name>? [Y/n]`. Yes sets it with
`hostnamectl` (keeping any domain part), updates `/etc/hosts`, and stops
cloud-init from restoring the old name at boot. It only asks in a terminal
without `--yes`; names with `_` aren't valid hostnames and are skipped.

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
| `--allowed-ips <cidrs>` | `CLIENT_ALLOWED_IPS` | `0.0.0.0/0, ::/0` (all traffic) | default for new clients (per client on `add`/`rotate`); `"10.4.5.0/24, fd…::/64"` (your subnets) for VPN-network only |
| `--subnet <cidr>` | `WG_SUBNET` | random `10.<1-10>.<1-10>.0/24` | clients' IPv4 network (server is `.1`); first setup asks, Enter takes the random one |
| `--subnet6 <cidr>` | `WG_SUBNET6` | random `fd…::/64` | clients' IPv6 network; `off` disables client IPv6 |
| `--mtu <bytes>` | `WG_MTU` | `1280` | tunnel MTU for server and clients (1280 minimum, at most `--warp-mtu`) |
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
| `--yes`, `-y` | everything | skip the per-step prompts; `setup` still asks to confirm its settings (unless there's no terminal) |
| `--force` | `setup` | regenerate server keys, reinstall wgcf, regenerate the WARP profile, take over configs these tools didn't create |
| `--purge` | `rollback` | also delete keys, clients and the WARP account |
| `--qr` / `--no-qr` | `wg-client.sh add`, `rotate`, `show` | force or skip the QR code |

`help` on either command lists everything.

### What setup does

Setup runs in two stages:

1. **WARP tunnel:** installs `wgcf`, registers a free WARP account, brings up
   the `warp` interface and tests it. (Registration refused? See
   [Register WARP on another machine](#register-warp-on-another-machine).)
2. **WireGuard server:** creates `wg0` with server keys, NAT for both ways out
   (WARP and the public interface), forwarding and systemd units, with WARP on,
   plus the [WARP watchdog](#how-it-works).

If WARP can't be set up on a first install, setup still finishes, with WARP
off; fix it, then `./wg-setup.sh warp on`.

**Open UDP 51820** (or your `--port`) in your VPS provider's firewall /
security group. The script opens it on the server itself (ufw or iptables) but
can't reach the provider's firewall.

To change a setting, re-run setup with the new flag, e.g.
`./wg-setup.sh setup --port 51821`. A change that affects clients (`--name`,
`--endpoint`, `--port`, `--dns`, `--mtu`, `--subnet`, `--subnet6`) rewrites
their configs (renames them for `--name`) and lists which to re-send; keys
don't change.

Changing `--subnet` with clients present moves them: each keeps its host
number and keys (`--subnet 192.168.250.0/24` turns `10.4.5.5` into
`192.168.250.5`), and allowed-IPs entries naming the old subnet are updated.
Their configs are rewritten, so re-send them. The new subnet must be at least
as large as the old one.

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
./wg-client.sh add printer --allowed-ips "10.4.5.0/24, fdXX:XXXX:XXXX::/64" # split tunnel: VPN network only (your subnets)
./wg-client.sh list                                  # address, last seen, traffic, allowed IPs; then pick one to act on
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

In a terminal, `list` then asks for a client number and offers its actions
(show, QR, share, rotate, disable/enable, remove). It runs the one you choose
and exits; Enter at either prompt exits without doing anything. With `--yes`
or when piped, `list` only prints the table.

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
| scp | `scp root@<server>:/etc/wireguard/clients/dengar.laptop.conf .` (`add` and `show` print the exact command) | your own machines |

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

## Exit cities

A client can leave through a city of your choosing instead of the server's
own location: its traffic goes into a WARP tunnel that itself runs inside a
Proton VPN tunnel to that city, so Cloudflare places it there and sites see a
Cloudflare IP in that city (fewer CAPTCHAs and blocks than a VPN's own IPs;
Proton sees only encrypted WARP traffic).

```
phone -> wg0 (this server) -> wx-par (WARP) -> inside px-par (Proton, Paris) -> Cloudflare, Paris
```

**Add a city.** In your Proton account: Downloads -> WireGuard configuration,
platform Linux, NetShield off, Moderate NAT / NAT-PMP / VPN Accelerator off,
pick a server in the city, Create, download. Each config is one key that can
run on one server only, so download one per server per city. Then on the
server:

```bash
./wg-setup.sh exit add par proton-FR-252.conf --label "Paris, FR"
./wg-setup.sh exit list
./wg-setup.sh exit test            # bring each one up and check it
./wg-setup.sh exit remove par      # refused while clients use it
```

or from your machine: save it under `local/exits/<server>/` with any name
(e.g. `paris.france.conf`) and run `scripts/servers.sh exit-add <server>`
(pick one, or `all`).
The first push of a file asks what it is ("Paris, France") and suggests a code
(`par-fr`: city + Proton's country tag); the answer is kept in
`local/exits/details.tsv`.
The code (`par`) is 2-6 of a-z0-9. `exit add` registers a separate WARP account
for the city, wires its routing into `wg0` live (clients stay connected), and
checks the result: `exit 'par' works: ip=104.28.x.x colo=CDG loc=FR warp=on`.

**Clients.** A client of a city is named `<code>.<name>`. `add` asks which
exit(s) when cities exist, or take them with `--exit`:

```bash
./wg-client.sh add laptop --exit warp,par   # laptop (default exit) and par.laptop
./wg-client.sh add par.tablet
```

Each is a separate client with its own key, address and config
(`dengar.par.laptop.conf`, whose `# exit:` line names the city). Import both on
the device and switch exits by switching tunnels in the WireGuard app.
`list` and `status` show each client's exit. The Android app takes tunnel
names up to 15 characters; `add` says when one is longer.

**On demand.** A city is only up while one of its clients is connected: the
exit watchdog (every 10s) brings it up when a client handshakes, and takes
it down after 5 minutes with none, so an idle city holds no Proton connection.
A device switching to a city's tunnel waits about 10-15 seconds the first
time. While a city is down, or broken, its clients are blocked (never sent out
another way); an up city whose WARP stops handshaking is restarted. `test`
checks each city's routing, and the tunnels when it's up.

## Check, back up, restore, undo

```bash
./wg-setup.sh test              # PASS/FAIL checks; non-zero exit on any FAIL
./wg-setup.sh status            # settings (and any not running yet), interfaces, clients, routes, rules
./wg-setup.sh log [lines] [-f]  # its changes, both watchdogs and the tunnels, in one log
./wg-setup.sh backup            # /var/backups/wireguard-tools/wireguard-tools.<host>.<date>.tar.gz
./wg-setup.sh restore <archive> # set this server up from a backup
./wg-setup.sh rollback          # stop everything, remove all rules, restore original files
./wg-setup.sh rollback --purge  # ...and delete keys, clients and the WARP account
```

`test` checks: WARP is up and handshaking, traffic through `warp` shows
`warp=on` while the server's own traffic shows `warp=off`, the server's
default route never changed, `wg0` is listening, forwarding is on, the WARP
rule is there exactly once (WARP on) or absent (off), every NAT/firewall rule
exists exactly once, the services start at boot in the right order, the WARP
watchdog timer is running, and whether any client has connected. With WARP off, WARP problems are warnings.
With exit cities: each city's routing (blackhole fallbacks, one rule per
client address) and, if it's up, both its handshakes and a WARP trace through it.

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
(so it doesn't need to register again), then runs setup. It won't overwrite a
server that's already set up (`rollback` or `rollback --purge` first).

The new server records its own default route and public interface, and an IP
endpoint is re-detected. If the address changed, every client config's
`Endpoint` is updated and the clients to re-send are listed (`wg-client.sh
share` or `show --qr`); their keys don't change. This also happens whenever a
re-run changes `--endpoint` or `--port`. An endpoint set as a hostname
(`--endpoint vpn.example.com`) is kept, so pointing that DNS name at the new
server means clients need nothing.

Backups go in `/var/backups/wireguard-tools/` (mode 700, outside the checkout,
so re-cloning the tools keeps them; `rollback` restores originals from its
`files/`); `backup <dir>` writes elsewhere. Each `backup` also prunes old
archives and file copies. A backup holds private keys (it's created mode 600);
keep it somewhere safe. Stop the old
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
same host number as its IPv4 address (`10.4.5.5` → `fd…::5`). Its IPv6 is
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
| `/etc/sysctl.d/99-wg.conf` | `net.ipv4.ip_forward = 1`, plus IPv6 forwarding (and `accept_ra = 2` on the public interface) when IPv6 is on |
| `/etc/systemd/system/wg-quick@wg0.service.d/` | start `wg0` after `warp` |
| `/usr/local/sbin/wg-tools-warp-watchdog`, `/etc/systemd/system/wg-tools-warp-watchdog.{service,timer}` | WARP watchdog |
| `/etc/wireguard/exits.db` | exit cities: `code index label` |
| `/etc/wireguard/exits/<code>.proton.conf`, `exits/<code>/` | a city's Proton config and its own WARP account |
| `/etc/wireguard/px-<code>.conf`, `wx-<code>.conf` | a city's Proton tunnel and the WARP tunnel inside it |
| `/usr/local/sbin/wg-tools-exit-watchdog`, `/etc/systemd/system/wg-tools-exit-watchdog.{service,timer}` | exit watchdog (cities up on demand) |

On your machine, `local/` (untracked) holds `servers`, `exits/<server>/*.conf`,
`exits/details.tsv` (which local file is which exit: server, file, code, full
name) and `backups/<server>/`.

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
| `exit-watchdog.sh.tmpl`, `exit-watchdog.service.tmpl`, `exit-watchdog.timer.tmpl` | the exit watchdog script and its systemd units |

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
- **Some sites hang with WARP on:** MTU is already 1280 (the lowest allowed)
  and TCP MSS is clamped; try `./wg-setup.sh warp off` to see if it's WARP's path.
- **Handshake works but nothing loads:** the device's config is out of date
  (e.g. after a `--subnet` change) or its `Address` is mistyped; re-send it.
- **Anything else:** `./wg-setup.sh status` (marks saved settings still
  pending, i.e. not running yet) and `./wg-setup.sh test`.

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
tests WARP and switches clients to it. Use these files on that server only:
they are one WARP device.
