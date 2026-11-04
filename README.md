# wireguard-tools

## Overview

wireguard-tools turns a bare Debian 11+ / Ubuntu 20.04+ server (amd64 or
arm64) into a WireGuard VPN server and manages its clients, on the server
itself or, for several servers, from your own machine.

A plain WireGuard server sends its clients out through its own IP. This one
can also send them through [Cloudflare WARP](https://1.1.1.1/) (Cloudflare's
free VPN service; the server connects to it as one WARP "device"), and through
a **jump**: another WireGuard server you add, such as a VPN provider's server
in another city (Proton VPN works well) or a second server of your own.

Each device picks where its traffic comes out, and whether through WARP:

| | without WARP | through WARP |
|---|---|---|
| **this server** | the server's IP: a plain, fast VPN | a Cloudflare IP near the server |
| **a jump** | the jump's IP | a Cloudflare IP near the jump (Paris, say) |

A device gets a tunnel for each combination you give it and switches between
them in its WireGuard app; nothing is switched on the server (see
[Exits and config names](#exits-and-config-names)).

**Bring your own WARP.** A device on a tunnel without WARP can run WARP itself,
inside its tunnel to this server (for a Mac:
[wireguard-tunnels](https://github.com/cgmckeever/wireguard-tunnels)' warped
tunnels). Sites still see a Cloudflare IP near the server or jump, but the
WARP account is the device's own and this server only carries encrypted WARP.

### Why route through Cloudflare?

A VPS's public IP is a datacenter address, and sites treat those with
suspicion: more CAPTCHAs, "unusual traffic" pages and outright blocks, as
they do with commercial VPN ranges. Through WARP, configs leave from
Cloudflare's network instead:

- **Sites see a Cloudflare IP, not your VPS.** WARP addresses are shared by
  millions of ordinary WARP users, so they're rarely blocked, and your server's
  own IP (tied to your hosting account) never shows up in their logs.
- **Your provider sees one encrypted tunnel to Cloudflare** (or, through a
  jump, to the jump), not which sites your clients visit.
- **IPv6 for clients even on an IPv4-only VPS**, through WARP's own IPv6.
- **You pick the location.** WARP exits near where it's entered: this server,
  or a [jump](#jumps) in another city (sites see Paris, say), still looking
  like an ordinary Cloudflare user rather than a VPN.
- **Fail closed:** if WARP or a jump goes down, its clients are blocked rather
  than quietly falling back to the VPS's IP.

The trade-offs: an extra hop (a few milliseconds for WARP, as Cloudflare is
close to most datacenters; more for a distant jump), Cloudflare sees where
traffic goes instead of your provider, and a few sites block WARP too;
the device's plain config is one tap away. It hides your server, not you: it isn't
an anonymity tool.

## Quick start

On a fresh server, as root:

```bash
apt-get update && apt-get install -y git
git clone https://github.com/cgmckeever/wireguard-tools.git /opt/wireguard-tools
/opt/wireguard-tools/bin/wgt setup --name myvpn     # from then on, just: wgt ...
```

1. **Open UDP 51820** in your VPS provider's firewall or security group. Setup
   opens it on the server itself but can't reach the provider's firewall; this
   is the usual reason clients can't connect.
2. **Add a client:** `wgt client add phone` prints a QR code; scan it in the
   WireGuard app.
3. **Check it:** `wgt summary` (or `wgt test`).

To manage one or more servers from your own machine instead of logging in to
each, see [Fleet management](#fleet-management).

### Basic commands

There are two:

- **`wgt`** runs as root on the server: `setup`, `summary`,
  `status`, `test`, `log`, `backup`, `restore`, `rollback`, plus `wgt client …` (`add`, `list`,
  `show`, `share`, `rotate`, `rename`, `disable`, `enable`, `remove`) and
  `wgt jump …` for jumps (`wgt exit …` still works).
- **`wgt-ctl`** runs on your own machine and drives your servers over SSH,
  mostly as `wgt-ctl SERVER COMMAND`: the server first, then any `wgt` command
  (`wgt-ctl boba client add phone`). It also sets up new servers, pulls
  updates, pushes jumps from local files and fetches backups
  ([Fleet management](#fleet-management)).

Both are safe to re-run, back up every file they change, take `--dry-run` for
anything that changes a server (with `wgt-ctl`, pass it to the `wgt` command:
`wgt-ctl boba setup --dry-run`),
and never print private keys except inside a config's QR code (shown by
`client add` and `rotate`, or `client show --qr`). They're Python 3.8+ with
only the standard library, so a server needs nothing extra.

In this README, `dengar` and `boba` are example server names, and `<server>`
stands for yours.

## How it works

The server has two WireGuard interfaces: `wg0`, which your clients connect to,
and `warp`, its own tunnel to Cloudflare.

**WARP.** `wgcf` (an unofficial command-line client for WARP) registers a free
WARP account and produces a WireGuard profile, written as
`/etc/wireguard/warp.conf` with `Table = off`, so the server's own routing is
never touched and SSH stays reachable. Routing table 100 (`--table`) holds
a default route through `warp` plus a blackhole fallback. `warp.conf` re-adds
the route through `warp` whenever it comes up, so restarting WARP on its own
doesn't cut clients off. `wg-quick@wg0` starts after `wg-quick@warp` (and
wants it, without requiring it).

**The WARP rule.** NAT and forwarding rules exist for both ways out (`warp`
and the public interface); each only matches its own outgoing interface. One
policy rule per address family, `ip rule from <VPN subnet> lookup 100`
(priority 10000), sends `warp.<device>` configs to table 100: out through
`warp`, or into the blackhole if WARP is down (fail closed). Every other
config has its own rule ahead of it (below).

**WARP watchdog.** Cloudflare occasionally stops answering the WARP tunnel's
handshakes (seen after some reboots: packets go out, nothing comes back, and
`warp.*` configs are blocked). A new source port fixes it within seconds.
`wg-tools-warp-watchdog.timer` checks every 30s; if `warp` has had no
handshake for 180s on two checks in a row, it moves `warp` to a new source
port (`wg set warp listen-port 0`, no restart) and logs it
(`wgt log`). `status` shows the last time it acted.

**Jumps.** Each jump `<code>` (e.g. `par-fr`) has two tunnels: `px-<code>`,
to the jump's WireGuard server, and `wx-<code>`, its own WARP tunnel running
inside it. Each jump has an index *n* (in `exits.db`). Its WARP table
`200+n` holds a default route through `wx-<code>` and a blackhole fallback;
one rule per config address (`from 10.4.5.7 lookup 201`, priority 9500, ahead
of the WARP rule) sends that config's traffic there. The jump's WARP tunnel marks its
own packets (fwmark `0x5700+n`), and a rule sends marked packets to table
`230+n`: a default route through the jump tunnel `px-<code>`, again with a
blackhole fallback, so the jump's WARP can never connect from the server's own
IP. The tunnels use `Table = off` and aren't enabled at boot; the jump
watchdog (`wg-tools-exit-watchdog.timer`, every 10s) starts and stops them.
The tables, rules and blackholes live in `wg0.conf` like everything else, so
they're there even while a jump is down.

**Configs without WARP.** A plain jump config (`par-fr.laptop`) has its rule
point at the jump table `230+n` instead (NATed out `px-<code>`; IPv4 only, its
IPv6 is blackholed). A plain host config (`laptop`) gets
`from <address> lookup main` (this server's own routes) plus a blackhole rule
right behind it (priority 9501), so if the server has no route (IPv6 on an
IPv4-only server) the traffic stops there instead of falling through to the
WARP rule.

| | `warp.` | plain |
|---|---|---|
| **host** | table 100 (the subnet's WARP rule) | `main`, then blackhole |
| **jump** `<code>` | table `200+n` (`wx-<code>`) | table `230+n` (`px-<code>`) |

**Always.** All routing/NAT/firewall rules live in `wg0.conf`'s
`PostUp`/`PostDown`, generated from one list so `PostDown` and `rollback` undo
exactly what `PostUp` did. Forward rules are inserted at the top so ufw/Docker
can't block them, and TCP MSS is clamped to avoid MTU stalls.

**IPv6.** Each config also gets an IPv6 address in a private `fd…::/64`
(picked at random once at setup and saved; `--subnet6` sets your own), with the
same host number as its IPv4 address (`10.4.5.5` → `fd…::5`). Its IPv6 is
NATed out the same way as IPv4: through WARP via WARP's own IPv6, which
works even on an IPv4-only server, with the same table 100 route and blackhole
fallback; plain host configs out the public interface, if the server has IPv6
(otherwise clients get IPv4 only). `wgt status` shows which. Clients'
`AllowedIPs` include `::/0` either way, so their IPv6 can't bypass the tunnel.
Turning IPv6 on for an existing server (`wgt setup --subnet6 <cidr>`, or a
plain re-run if it was off only because the kernel had IPv6 disabled) adds the
address to every config; re-send them. `--subnet6 off` disables it.

### Files

| Path | What |
|------|------|
| `/etc/wireguard/wg0.conf` | server interface + rules; `[Peer]`s rebuilt from `clients.db` |
| `/etc/wireguard/warp.conf` | WARP tunnel |
| `/etc/wireguard/wgcf/` | WARP account and profile |
| `/etc/wireguard/server.key`, `server.pub` | server keypair |
| `/etc/wireguard/clients.db` | one line per config: `name address public-key allowed-ips`, no private keys |
| `/etc/wireguard/clients.disabled` | names of disabled configs |
| `/etc/wireguard/psk/<name>.psk` | per-config preshared keys |
| `/etc/wireguard/clients/<server>.<name>.conf` | tunnel configs (the only copy of each one's private key) |
| `/etc/wireguard/wg-tools.state` | saved settings (including `WARP=on|off`), recorded default route |
| `/etc/sysctl.d/99-wg.conf` | `net.ipv4.ip_forward = 1`, plus IPv6 forwarding (and `accept_ra = 2` on the public interface) when IPv6 is on |
| `/etc/systemd/system/wg-quick@wg0.service.d/` | start `wg0` after `warp` |
| `/etc/systemd/system/wg-tools-warp-watchdog.{service,timer}` | WARP watchdog (runs `wgt watchdog warp` every 30s) |
| `/etc/wireguard/exits.db` | jumps: `code index label` |
| `/etc/wireguard/exits/<code>.proton.conf`, `exits/<code>/` | a jump's WireGuard config as given (any provider; the file name is historical) and its own WARP account |
| `/etc/wireguard/px-<code>.conf`, `wx-<code>.conf` | a jump's tunnel and the WARP tunnel inside it |
| `/etc/systemd/system/wg-tools-exit-watchdog.{service,timer}` | jump watchdog (runs `wgt watchdog exits` every 10s: jumps up on demand) |
| `/usr/local/sbin/wgt` | link to the checkout's `bin/wgt` |

Secrets are mode 600 in 700 directories. Before any file is changed, a
timestamped copy goes to `/var/backups/wireguard-tools/files/<its path>` (e.g.
`files/etc/wireguard/clients.db.<date>.bak`); `rollback` restores the
originals. Each file keeps its original plus the 5 newest copies, and
`wgt backup` keeps the 10 newest archives.

### Templates

These files are rendered from `templates/`, filling `{{PLACEHOLDER}}`s:

| Template | Becomes |
|----------|---------|
| `client.conf.tmpl` | each `/etc/wireguard/clients/<server>.<name>.conf` |
| `wg-quick-after-warp.conf.tmpl` | the systemd drop-in that starts `wg0` after `warp` |
| `watchdog.service.tmpl`, `warp-watchdog.timer.tmpl`, `exit-watchdog.timer.tmpl` | the watchdogs' systemd units |

Edit them to change what every new client gets (e.g. `PersistentKeepalive`).
Changes apply to clients added or rotated afterwards. Every placeholder must
stay: a missing value stops the command before anything is written.
Edit them in your own copy of the repository (a fork) and push, not on a
server: servers update with a fast-forward-only pull, which stops (and changes
nothing) on local edits.
`wg0.conf`, `warp.conf` and the jump tunnels (`px-`/`wx-<code>`) are built in code (`wgt/render.py`,
`wgt/rules.py`), since their contents depend on the WARP setting, the routing
rules and the client list.

## Server management

Everything in this part runs on a server, as root: `wgt …`. From your own
machine, `wgt-ctl SERVER …` runs the same commands on any server (see
[Fleet management](#fleet-management)).

### Set up a server

#### Install

Nothing else is needed on the server: `wgt setup` installs its own packages
(WireGuard, iptables, curl, ping, qrencode, magic-wormhole) and links
`/usr/local/sbin/wgt` to the checkout.

```bash
sudo -i   # everything below runs as root
apt-get update && apt-get install -y git
git clone https://github.com/cgmckeever/wireguard-tools.git /opt/wireguard-tools
/opt/wireguard-tools/bin/wgt setup --name dengar      # from then on: wgt ...
```

Then **open UDP 51820** (or your `--port`) in your VPS provider's firewall /
security group. Setup opens it on the server itself (ufw or iptables) but
can't reach the provider's firewall.

Or from your own machine, `wgt-ctl init` does all of this over SSH (set up
`wgt-ctl` first: see [Fleet management](#fleet-management)). Update later with
`git -C /opt/wireguard-tools pull --ff-only` (or `wgt-ctl <server> pull`); see
[Updating servers](#updating-servers) for when an update also needs `wgt setup`.

#### Setup

```bash
wgt setup --name dengar              # typical: just name it
wgt setup --name dengar --dry-run    # see exactly what it would change first
```

The first setup asks for the client subnet, offering a random
`10.<1-10>.<1-10>.0/24` that doesn't overlap a route the server already has
(`Client subnet [10.4.5.0/24]:`; Enter takes it). `--subnet` sets your own
instead; `--yes` or `--dry-run` skip the question and take the random one.

Each run lists the settings and where each came from (flag, env, entered,
saved, default, detected, random), shows what changed since the last setup,
and asks `Continue? [Y/n]` before changing anything. `--yes` doesn't skip this
question (only a run without a terminal, like a script, does); `--dry-run`
shows it and stops.

If the server name (`--name`) differs from the machine's hostname, setup then
asks `Update hostname <current> to <name>? [Y/n]`. Yes sets it with
`hostnamectl` (keeping any domain part), updates `/etc/hosts`, and stops
cloud-init from restoring the old name at boot. It only asks in a terminal
without `--yes`; names with `_` aren't valid hostnames and are skipped.

#### Flags

All optional. Settings are saved in `/etc/wireguard/wg-tools.state`, so
re-runs and `wgt client` commands reuse them; pass a flag again to change it. Each can
also be set as an environment variable (flag > env > saved > default).

| Flag | Env | Default | What it sets |
|------|-----|---------|--------------|
| `--name <name>` | `SERVER_NAME` | short hostname | server name; tunnel configs are `<name>.<config>.conf` |
| `--port <port>` | `WG_PORT` | `51820` | UDP port clients connect to |
| `--endpoint <host-or-ip>` | `ENDPOINT` | detected public IPv4 | address written into tunnel configs (a hostname lets you move servers without re-sending configs) |
| `--dns <ip>` | `WG_DNS` | `1.1.1.1` | DNS server given to clients |
| `--allowed-ips <cidrs>` | `CLIENT_ALLOWED_IPS` | `0.0.0.0/0, ::/0` (all traffic) | default for new configs (per config on `add`/`rotate`); `"10.4.5.0/24, fd…::/64"` (your subnets) for VPN-network only |
| `--subnet <cidr>` | `WG_SUBNET` | random `10.<1-10>.<1-10>.0/24` | clients' IPv4 network (server is `.1`); first setup asks, Enter takes the random one |
| `--subnet6 <cidr>` | `WG_SUBNET6` | random `fd…::/64` | clients' IPv6 network; `off` disables client IPv6 |
| `--mtu <bytes>` | `WG_MTU` | `1280` | tunnel MTU for server and clients (1280 minimum, at most `--warp-mtu`) |
| *Rarely needed:* | | | |
| `--warp-mtu <bytes>` | `WARP_MTU` | `1280` | MTU of the WARP tunnel |
| `--wg-if` / `--warp-if` | `WG_IF` / `WARP_IF` | `wg0` / `warp` | interface names |
| `--table <n>` | `RT_TABLE` | `100` | routing table used for WARP |
| | `WGCF_VERSION` | `latest` | wgcf release to install, e.g. `v2.2.22` |
| | `WGCF_SHA256` | | pin the wgcf binary's checksum |

Example: `wgt setup --name dengar --port 51821 --endpoint vpn.example.com`.

Flags for one run (not saved), for setup and other commands:

| Flag | Works with | Does |
|------|-----------|------|
| `--dry-run` | everything | show what would change; change nothing |
| `--yes`, `-y` | everything | skip the per-step prompts; `setup` still asks to confirm its settings (unless there's no terminal) |
| `--force` | `setup` | reinstall wgcf, regenerate the WARP profile, take over configs these tools didn't create |
| `--new-server-keys` | `setup` | a new server keypair: every client stops working until rotated (`wgt client rotate`) |
| `--purge` | `rollback` | also delete keys, clients, the WARP accounts and jumps |
| `--qr` / `--no-qr` | `wgt client add`, `rotate`, `show` | force or skip the QR code |
| `--exit host\|<jump>[,…]` | `wgt client add` | the exit: `host` (the WireGuard server; `warp` is the old spelling) or a jump code; a comma list makes one config per exit |
| `--warp on\|off` | `wgt client add` | only the `warp.` config (`on`) or only the plain one (`off`); both by default |
| `--label "<name>"` | `wgt jump add` | the jump's full name, e.g. `"Paris, France"` |
| `--location "<place>"` | `setup` | where this server is, shown as `host:<place>`; by default its city, looked up once (ipinfo.io) |
| `--no-follow` | `wgt log` | print the last lines and stop (also when piped) |

`wgt help` and `wgt-ctl help` list everything.

#### What setup does

Setup runs in two stages:

1. **WARP tunnel:** installs `wgcf`, registers a free WARP account, brings up
   the `warp` interface and tests it. (Registration refused? See
   [Register WARP on another machine](#register-warp-on-another-machine).)
2. **WireGuard server:** creates `wg0` with server keys, NAT for both ways out
   (WARP and the public interface), forwarding and systemd units, plus the
   [WARP watchdog](#how-it-works). Jumps, if any, get their configs refreshed.

If WARP can't be set up, setup still finishes: plain configs work, `warp.*`
ones are blocked until it does; fix it and re-run setup.

**Servers from before config names carried WARP:** the first setup on the new
version renames each config that went through WARP to its `warp.` name (same
keys and address, so devices keep working; rename the tunnel on the device if
you like) and drops the old `clients.nowarp` and `wgt warp` setting, once.

To change a setting, re-run setup with the new flag, e.g.
`wgt setup --port 51821`. A change that affects clients (`--name`,
`--endpoint`, `--port`, `--dns`, `--mtu`, `--subnet`, `--subnet6`) rewrites
their configs (renames them for `--name`) and lists which to re-send; keys
don't change.

Changing `--subnet` with clients present moves them: each keeps its host
number and keys (`--subnet 192.168.250.0/24` turns `10.4.5.5` into
`192.168.250.5`), and allowed-IPs entries naming the old subnet are updated.
Their configs are rewritten, so re-send them. The new subnet must be at least
as large as the old one.

### Jumps

A jump is another WireGuard server that clients can leave through instead of
this one: a VPN provider's server in a city you pick, or a server of your own.
Through `warp.par-fr.<device>`, the traffic goes into a WARP tunnel that itself runs
inside the jump tunnel, so Cloudflare places it near the jump: sites see a
Cloudflare IP there (fewer CAPTCHAs and blocks than a VPN's own IPs), and the
jump sees only encrypted WARP traffic. Through `par-fr.<device>`, sites see the jump's IP.

```
warp.par-fr.phone: phone -> wg0 (this server) -> wx-par-fr (WARP) -> inside px-par-fr (jump, Paris) -> Cloudflare, Paris
par-fr.phone:      phone -> wg0 (this server) -> px-par-fr (jump, Paris) -> sites
```

Any WireGuard config with one peer and an IPv4 address works (it's the
jump's client config: this server connects to the jump as a client). Each is
one key: use it on one of *your* servers only.

**Proton VPN** is a good source of jumps in many cities. At
account.protonvpn.com: Downloads -> WireGuard configuration; platform Linux;
NetShield off; Moderate NAT, NAT-PMP and VPN Accelerator off; pick a server in
the city; Create; download (one per server per city).

**Add the jump**, from your machine with `wgt-ctl` (see
[Fleet management](#fleet-management)) or on the server:

```bash
# your machine: save it as local/exits/<server>/<any name>.conf, then
wgt-ctl boba jump-add                    # pick it by number (or: all)

# or on the server: copy it there first (scp paris.conf root@<server>:), then
wgt jump add par-fr paris.conf --label "Paris, France"
wgt jump add par-fr - --label "Paris, France" < paris.conf    # or read it from stdin
```

The first time `jump-add` sees a file it asks what it is ("Paris, France")
and suggests a code from the name and the config's country tag if it has one
(Proton's `FR#252` gives `par-fr`); the answer is kept in
`local/exits/details.tsv`, and the file keeps its own name. The copy on the
server records both: it starts with `# exit par-fr: Paris, France` and
`# local file: paris.conf`. A code is 2-6 of a-z, 0-9 and `-`, starting
with a letter, not ending in `-` (and not `host` or `warp`).

`jump add` registers a separate WARP account for the jump, wires its routing
into `wg0` live (clients stay connected), and checks the result:
`jump 'par-fr' works: ip=104.28.x.x colo=CDG loc=FR warp=on` (`colo` is
Cloudflare's data centre: CDG is Paris). Adding the same code again replaces
its config.

```bash
wgt jump list              # jumps: state (up, up without WARP, idle), configs, endpoint
wgt jump test              # bring each one up and check it
wgt jump remove par-fr     # refused while configs use it
```

**On demand.** A jump is only up while one of its configs is connected: the
jump watchdog (every 10s) brings it up when one handshakes and takes it
down after 5 minutes with none, so an idle jump holds no connection. A device
switching to a jump's tunnel waits about 10-15 seconds the first time. While a
jump is down, or broken, its configs are blocked, never sent out another way.
The jump's WARP is only started when a `warp.` config needs it. A jump tunnel that
stops handshaking is restarted; a WARP inside it that does gets a new source
port, then a restart of the WARP alone.

Configs get their exit when they're added: see
[Exits and config names](#exits-and-config-names).

### Manage clients

```bash
wgt client add laptop                            # full tunnel (default); prints a QR code
wgt client add printer --allowed-ips "<your subnets>"  # split tunnel: VPN network only (subnets: wgt status)
wgt client list                                  # address, last seen, traffic, allowed IPs
wgt client show laptop                           # config with keys redacted
wgt client show laptop --qr                      # QR code again (phones: WireGuard app → scan)
wgt client share laptop                          # send the config via magic-wormhole (one-time code)
wgt client rotate laptop                         # new keys, same address; old config stops working
wgt client disable laptop                        # cut it off now; keys, config and address kept
wgt client enable laptop                         # let it back in with the same config
wgt client rename laptop work-laptop             # same keys and address; its config keeps working
wgt client remove laptop
```

Each command takes a config name (`warp.laptop`, `par-fr.laptop`; see
[Exits and config names](#exits-and-config-names)). Leave it out on
`rotate`, `remove`, `disable`, `enable`, `show` or `share` to pick from a
numbered list (`wgt client` alone is `list`). Changes apply live; other configs
stay connected.

In a terminal, `list` then asks for a config number and shows its actions as a
numbered menu (show, QR, share, rotate, disable/enable, rename, remove). It runs
the one you choose and exits; Enter at either prompt exits without doing
anything. With `--yes`
or when piped, `list` only prints the table.

`disable` removes the config's peer from the running `wg0` and leaves it out of
`wg0.conf`, so it stays off across restarts; `list` shows it as `disabled`.
Nothing on the device changes: its tunnel just gets no handshake (with a
full-tunnel config, no internet until its tunnel is switched off). `enable`
puts it back; the same config works, nothing to re-send. Disabled configs are
listed in `/etc/wireguard/clients.disabled`.

Each config gets its own keypair, a preshared key and the next free address.
Its config is written to `/etc/wireguard/clients/<server>.<name>.conf` (e.g.
`dengar.warp.laptop.conf`), so configs from different servers don't collide.

#### Exits and config names

A client is a device; it gets one tunnel config per combination of exit and
WARP (see [Overview](#overview)), and the config's name says which:
`[warp.][<jump>.]<device>`.

| config | traffic goes | sites see |
|---|---|---|
| `laptop` | out of this server | the server's IP |
| `warp.laptop` | out of this server, through Cloudflare WARP | a Cloudflare IP near the server |
| `par-fr.laptop` | out of the `par-fr` jump | the jump's IP |
| `warp.par-fr.laptop` | out of the `par-fr` jump, through WARP | a Cloudflare IP near the jump |

`wgt client` commands use these names. The files add the server's name in
front, so `warp.laptop` on boba is `boba.warp.laptop.conf`.

`add <device>` makes the plain and the `warp.` config for each exit. It asks
which exit(s) when jumps exist, or take them with `--exit` (`host` is this
server; `--exit host,par-fr` covers both); `--warp on|off` keeps only one of
each pair, and a full config name adds just that one. Adding a jump makes no
configs: each device gets one when you add it. Through `wgt-ctl`, `add` first
offers to push jump configs you've staged in `local/exits/<server>/` but not
yet added, so they show up in that menu:

```bash
wgt client add laptop                             # laptop, warp.laptop
wgt client add laptop --exit host,par-fr          # ...plus par-fr.laptop, warp.par-fr.laptop
wgt client add mac --exit par-fr --warp off       # only par-fr.mac (e.g. the Mac runs its own WARP)
wgt client add warp.par-fr.tablet                 # exactly that one
wgt client rename warp.par-fr.tablet par-fr.tablet   # same keys; now without WARP, live
```

Each config is its own WireGuard peer with its own key and address (its
`# exit:` line says where it goes in words). Import the ones you want on the
device and switch exits by switching tunnels in the WireGuard app. A config's
name fixes its way out: to move one, rename it; its keys stay, so the device's
tunnel keeps working. `list` and `wgt-ctl clients` show each config with its
EXIT (`host:Atlanta`, the server's location, or `jump:Paris, France`) and WARP
(on/off) columns. The Android app only takes tunnel names up to 15
characters, so rename longer ones when importing there (`add` reminds you).

#### Getting a config onto a device

| How | Command | Best for |
|-----|---------|----------|
| Magic Wormhole | `wgt client share laptop`; recipient runs `wormhole receive <code>` | sending it to someone else, no SSH needed |
| QR code | printed by `add`/`rotate` in a terminal (`--no-qr` skips it); again any time with `wgt client show laptop --qr` | phones (WireGuard app → scan) |
| scp | `scp root@<server>:/etc/wireguard/clients/dengar.laptop.conf .` (`add` and `show` print the exact command) | your own machines |

`share` prints a one-time code (like `7-guitar-revenge-tidy`) and waits up to 10
minutes. The file is encrypted end to end; a wrong guess at the code ends the
transfer. The recipient needs magic-wormhole (`brew install magic-wormhole`,
`apt install magic-wormhole`, `pipx install magic-wormhole`) and then imports
the `.conf` into their WireGuard app.

In iTerm2 or WezTerm the QR code is drawn as an inline image 32 columns wide
(set `QR_WIDTH`, e.g. `QR_WIDTH=24`; `QR_WIDTH=0` for the text version, which
other terminals and tmux always get). The image is streamed, never written to disk.

Both the QR code and the wormhole transfer carry the config's private key:
send them only to that device's owner.

### Check, back up, restore, clean up

```bash
wgt test              # PASS/FAIL checks; non-zero exit on any FAIL
wgt summary           # at a glance: one line per fact, says what is wrong
wgt status            # settings (and any not running yet), interfaces, clients, routes, rules
wgt log [N]           # its changes, both watchdogs and the tunnels: last lines, then follows (Ctrl-C stops)
wgt backup            # /var/backups/wireguard-tools/wireguard-tools.<host>.<date>.tar.gz
wgt restore <archive> # set this server up from a backup
wgt rollback          # stop everything, remove all rules, restore original files
wgt rollback --purge  # ...and delete keys, clients, WARP accounts and jumps
```

`test` checks: WARP is up and handshaking, traffic through `warp` shows
`warp=on` while the server's own traffic shows `warp=off`, the server's
default route never changed, `wg0` is listening, forwarding is on, the WARP
rule is there exactly once, every NAT/firewall rule exists exactly once, the
services start at boot in the right order, the WARP watchdog timer is running,
the `main` and blackhole rules of plain host configs, and whether any config
has connected. For each jump: its routing (blackhole fallbacks,
one rule per config address, to the right table for its WARP setting), the
jump watchdog, and, if it's up, its handshakes and a WARP trace through it
(only the jump tunnel's handshake while it runs without WARP).

#### Moving to a new server (or rebuilding)

```bash
# old server
wgt backup                          # or, from your machine: wgt-ctl <old> backup

# your machine: fetch it, then put it on the new server (or use wormhole send/receive)
scp root@<old>:/var/backups/wireguard-tools/wireguard-tools.*.tar.gz .
scp wireguard-tools.<host>.<date>.tar.gz root@<new>:

# new server: clone only (the first lines of Install), do NOT run setup
/opt/wireguard-tools/bin/wgt restore wireguard-tools.<host>.<date>.tar.gz
```

`restore` brings over the server keys, every client (same keys, addresses and
allowed IPs), the settings, the WARP account (so it
doesn't need to register again) and the jumps, then runs setup. It won't overwrite a
server that's already set up (`rollback` or `rollback --purge` first).

The new server records its own default route and public interface, and an IP
endpoint is re-detected. If the address changed, every config's
`Endpoint` is updated and the clients to re-send are listed (`wgt client
share` or `show --qr`); their keys don't change. This also happens whenever a
re-run changes `--endpoint` or `--port`. An endpoint set as a hostname
(`--endpoint vpn.example.com`) is kept, so pointing that DNS name at the new
server means clients need nothing.

Backups go in `/var/backups/wireguard-tools/` (mode 700, outside the checkout,
so re-cloning the tools keeps them; `rollback` restores originals from its
`files/`); `backup <dir>` writes elsewhere. Each `backup` also prunes old
archives and file copies. A backup holds private keys (it's created mode 600);
keep it somewhere safe. Stop the old server once the new one works: both would
be using the same WARP devices and jump keys.

## Fleet management

### Your servers

`wgt-ctl` runs on your own machine (macOS or Linux, Python 3.8+) and drives the
servers over SSH. Clone this repository there too
(`git clone https://github.com/cgmckeever/wireguard-tools.git ~/wireguard-tools`),
then put its `bin/` on your PATH, or `alias wgt-ctl=~/wireguard-tools/bin/wgt-ctl`.
It needs SSH as root to each server, with your key (`init` can set that up).
The servers it knows are listed in `local/servers`, one per line: name, SSH
login, and optionally the checkout directory (default `/opt/wireguard-tools`).
Like everything under `local/`, it is untracked:

```
dengar  root@203.0.113.10
boba    root@198.51.100.20
```

### Commands

Most commands have the shape **`wgt-ctl SERVER COMMAND [arguments]`**.
`SERVER` is a name from `local/servers`, its number in the list, or `all`.
A few commands aren't about one server and take no `SERVER` (last group below).

**One server: `wgt-ctl SERVER COMMAND`.** Any `wgt` command runs on that server,
exactly as if you'd typed it there:

```bash
wgt-ctl boba summary                   # wgt summary on boba (likewise status, test, log, warp, setup, ...)
wgt-ctl boba client add phone --exit par-fr
wgt-ctl boba client                    # boba's client list and action menu
wgt-ctl boba jump list
```

and these run from your machine, against that server:

```bash
wgt-ctl boba pull                      # git pull its checkout (fast-forward only)
wgt-ctl boba backup                    # wgt backup there, fetched to local/backups/boba/
wgt-ctl boba jump-add                  # push a WireGuard config from local/exits/boba/ as a jump
wgt-ctl boba ssh                       # log in (or: ssh <command>)
wgt-ctl boba branch some-branch        # switch its checkout
wgt-ctl boba forget                    # drop it from local/servers; the server is untouched
wgt-ctl boba remove                    # tear it down: rollback --purge, delete its checkout,
                                       #   wgcf and backup archives, then forget it (asks for its name)
```

**Every server: `wgt-ctl all COMMAND`.** Runs on each server in turn; one
failing doesn't stop the rest. Takes any `wgt` command, plus `pull`, `backup`
and `branch`:

```bash
wgt-ctl all test
wgt-ctl all pull
wgt-ctl all backup
```

**No server.** Overviews of every server, and adding a new one:

```bash
wgt-ctl [list]                         # the servers: branch@commit (or unreachable), jump configs staged
wgt-ctl summary                        # every server at a glance (wgt summary on each, side by side)
wgt-ctl clients                        # every config on every server: exit, last seen, traffic
wgt-ctl init                           # new server: asks address + name, installs, runs setup, adds it
wgt-ctl init --address 1.2.3.4 --name lando --user root --branch main -- --port 51821
```

Leave `SERVER` out of a server command (`wgt-ctl status`) to pick one from the
list; `wgt-ctl summary` with no server is the overview of all of them. Because
these words come first, a server can't be named `init`, `clients`, `summary`,
`all`, `help` or `list`; a name starts with a letter. `scp` commands printed
through `wgt-ctl` use the login from `local/servers`, so you can paste them on
your machine.

### Adding a server

`init` logs in as root and, on first contact, shows the server's host key for
you to check against your provider's console. If only a password works it
offers `ssh-copy-id`. It then installs git, clones the same repository your
local checkout came from (its `origin`) and
runs `wgt setup --name <name>` with its usual prompts; anything after `--` is
passed to setup. The server is added to the list only once setup finishes. A
server already running these tools is adopted under its saved name instead.

### Private files

Private files stay on your machine under `local/`: jump configs in
`exits/<server>/` (see [Jumps](#jumps)), the table that
names them in `exits/details.tsv`, and archives fetched by `backup` in
`backups/<server>/` (they hold private keys).

### Updating servers

Servers run their checkout directly (the watchdog timers too, every few
seconds), so new code runs as soon as it's pulled; but configs already
written aren't rendered again until `wgt setup` runs. With `wgt-ctl`:

1. Check what would render differently on each server (read-only):
   `tools/render-diff.sh`. "identical" everywhere means a pull is all it takes.
2. Try one server first: `wgt-ctl boba pull && wgt-ctl boba test`.
3. Then the rest: `wgt-ctl all pull`.
4. If step 1 showed differences (a template, `wgt/render.py` or `wgt/rules.py`
   change, new packages or units), apply them with `wgt-ctl all setup --yes`.
   Setup restarts `wg0` only if its settings changed, not for comment-only
   changes.

A new `wgt` command or flag works on a server only once that server has
pulled; `wgt-ctl`'s own steps (`jump-add`, `clients`) stick to commands every
version knows.

On a single server without `wgt-ctl`: `git -C /opt/wireguard-tools pull --ff-only
&& wgt test`, then `wgt setup --yes` if the change touched templates or how
configs are rendered.

## Troubleshooting

- **Clients can't connect:** the provider firewall usually still blocks UDP 51820
  (see [Quick start](#quick-start)).
- **`wgcf register` fails:** WARP may refuse registrations from some server IPs.
  Retry later, or register on another machine and copy the account over
  ([below](#register-warp-on-another-machine)). If the WARP tests still fail
  (no handshake), the provider is blocking WARP's traffic: use plain configs.
- **Some sites hang through WARP:** MTU is already 1280 (the lowest allowed)
  and TCP MSS is clamped; try the device's plain config to see if it's WARP's path.
- **No internet through a jump:** `wgt jump test` says which layer
  fails. No jump handshake usually means the server was retired or the key
  revoked (Proton does both): get a new config and add it again (same code).
  `wgt log` shows the watchdog bringing jumps up and down.
- **Handshake works but nothing loads:** the device's config is out of date
  (e.g. after a `--subnet` change) or its `Address` is mistyped; re-send it.
- **Anything else:** `wgt status` (marks saved settings still
  pending, i.e. not running yet) and `wgt test`.

### Register WARP on another machine

If setup reports `wgcf register failed` (it then finishes; `warp.*` configs stay blocked),
register the WARP account somewhere WARP accepts (your Mac or a Linux machine;
on a Mac: `brew install wgcf`) and copy it over. Registration happens once, and is skipped when the
account file exists. The failed run already created `/etc/wireguard/wgcf/` for it.

```bash
# on your machine
mkdir -p ~/wgcf-vps && cd ~/wgcf-vps
wgcf register --accept-tos     # a new WARP device, separate from your Mac's WARP app
wgcf generate                  # uses the API too, so do it here as well
scp wgcf-account.toml wgcf-profile.conf root@<server>:/etc/wireguard/wgcf/
ssh root@<server> 'chmod 600 /etc/wireguard/wgcf/*'
cd ~ && rm -rf ~/wgcf-vps      # local copies hold the device's private key
```

Then re-run `wgt setup` on the server; it uses the copied account and
tests WARP. Use these files on that server only:
they are one WARP device.

## Development

The code is the `wgt/` package (Python 3.8+, standard library only); `bin/wgt`
and `bin/wgt-ctl` run it from the checkout.

```bash
./test.sh                  # all tests (first run sets up .venv with pytest, ruff, mypy); CI runs them on push
./test.sh -k exit          # just the tests matching "exit" (any pytest option works)
./test.sh lint             # ruff and mypy (settings in pyproject.toml)
tools/render-diff.sh       # read-only: what this checkout would render differently on each server
tools/trial.sh <jump.conf>     # on a throwaway server: the full cycle, back to clean (a jump in Paris)
```

[DEVELOPING.md](DEVELOPING.md) has the module map, the conventions and how the
tests work.
