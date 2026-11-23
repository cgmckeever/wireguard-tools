# tunnel-pi: a Raspberry Pi as a residential exit

Put a Raspberry Pi on a friend's home network, and chosen WireGuard clients of
your VPS reach the internet **from the friend's home IP**. The friend only
plugs in power and an ethernet cable: no router settings, no open ports.

```
client ──WireGuard──▶ your VPS ──WireGuard link (wgpi)──▶ Pi at the friend's ──▶ internet
                                  ▲ the Pi dials out and keeps it open, so the friend's NAT doesn't matter
```

Your clients connect to your VPS exactly as they do today. The VPS sends the
chosen ones' traffic down a link the Pi keeps open, and the Pi sends it out
under the friend's IP. You build and test everything at home; at drop-off the
Pi only needs power.

**Status:** the scripts are tested piece by piece, not yet end to end on a
real Pi and VPS. Run it first on a VPS nobody else depends on.

## Done when

1. A chosen client's public IP (`curl https://ifconfig.me`) is the friend's.
2. Its DNS lookups also leave from the friend's IP.
3. When the link is down, that client gets no connection at all. It never
   falls back to the VPS's IP.
4. Nothing from the tunnel reaches devices on the friend's LAN, unless you
   turn on LAN mode (2.3).
5. Clients that aren't chosen are unaffected.
6. You can reach the Pi from anywhere to check and change it.

Phase 3 tests each of these.

## Choose your setup

Three choices, made once in the settings file:

| Choice | Options | Pick |
|---|---|---|
| **Routing** (`TPI_ROUTING`): what sends clients into the Pi | `tpi`: tunnel-pi's own rules on the VPS, for **any** WireGuard VPS. You choose clients by their address (`CLIENT_IPS`) and make their configs yourself. `wgt`: for a VPS running **wgt** (the wireguard-tools in this repository). The Pi becomes an ordinary wgt jump, and wgt makes each device its own config | `wgt` if your VPS runs wgt, else `tpi` |
| **wgt-ctl** (`WGT_SERVER`), with `wgt` | If you manage the VPS with `wgt-ctl`, name it, and everything on the VPS goes through `wgt-ctl`: device numbers, configs delivered to `~/wg-tunnels` | set it if you use `wgt-ctl` |
| **Emergency door** (`SSH_CLOUDFLARE`) | Normally you reach the Pi through the VPS, over the link. That fails exactly when the link is down (a blocked port, a changed VPS IP, a bad change). A Cloudflare Tunnel gives SSH to the Pi that doesn't need the link. It costs a domain on Cloudflare (about $10–15 a year; the rest is free) and step 1.3. Without it, a dead link means the friend power-cycles the Pi, or you take it home | recommended; if you skip it, use `PI_PORT="443"` (rarely blocked) |

## What you need

- **A Raspberry Pi 4 or 5**, an SSD (or an SD card), a power supply, and
  ethernet at home.
- **A Linux VPS** running the WireGuard server your clients use, with
  `wg-quick`, `iptables` and systemd (Debian and Ubuntu have all three), and
  root ssh (or passwordless `sudo`). With `TPI_ROUTING=wgt`, wgt on it.
- **Your computer** (macOS or Linux) with `ssh`, and this folder.
- **For the emergency door:** a domain whose DNS is on Cloudflare, a Cloudflare
  Zero Trust account (free; signing up may ask for a card), and `cloudflared`
  on your computer (macOS: `brew install cloudflared`).

## How to read this

- **One command does the work:** `scripts/tpi-ctl.sh`, run on your computer in
  this folder. It ssh-es to the Pi and the VPS and runs `pi.sh` and `vps.sh`
  there. `scripts/tpi-ctl.sh` with no arguments lists its commands.
- **A setting's name stands for its value:** `ssh SSH_LAN` means
  `ssh pi@tunnel-pi.local`.
- **Every step ends with a Check.** Don't go on until it passes.
- **Safe to repeat:** every command can run again. Files change only when they
  must, and keys are kept. `TP_DRY_RUN=1 scripts/tpi-ctl.sh <command>` shows
  what would run without changing anything.
- **Markers** say where a step happens:

| Marker | Where |
|---|---|
| **[Computer]** | your machine, in this folder |
| **[Pi]** | a shell on the Pi |
| **[VPS]** | a root shell on the VPS |
| **[client]** | a device connected to the VPS |
| **[Browser]** | the Cloudflare dashboard |
| **[Hands]** | something physical: flashing, plugging in, a phone hotspot |

### For an AI agent following this

- **Ask before touching the VPS.** `link`, `all`, `client`, `undo`, and `exit`
  with `TPI_ROUTING=wgt` change it, and other people's traffic may run
  through it. Get the person's go-ahead for that server first, read-only
  commands included, and suggest a test server. `tpi-ctl.sh` also asks on
  the terminal; once the person has agreed for this VPS, `TP_YES=1` answers
  for you. Reaching the Pi through the VPS changes nothing there, but tell the
  person it connects. Follow any rules the person has about which servers you
  may use.
- **Some steps need a person:** everything marked **[Browser]** or
  **[Hands]**, the browser login on the first `ssh SSH_CLOUDFLARE`, making
  client configs with `TPI_ROUTING=tpi`, and typing a Wi-Fi password. Stop,
  say exactly what to do, and wait.
- **Dry run first:** `TP_DRY_RUN=1 scripts/tpi-ctl.sh all`.
- **Secrets stay put.** Never print a `*.key` file, a `PrivateKey` line, a jump
  file, the Cloudflare token or a Wi-Fi password. The scripts never do.
- **A FAIL means stop.** Report the command, its output and the matching
  Troubleshooting row. Don't improvise a fix on the VPS.

---

# Phase 1: prepare the Pi

## 1.1 Fill in the settings **[Computer]**

```sh
cp tunnel-pi.conf.example tunnel-pi.conf    # git ignores this name
```

Fill it in (each line has a comment), then check it:

| Setting | When | What to put |
|---|---|---|
| `SSH_LAN` | always | `user@hostname` for the Pi on your home network. You choose both in 1.2 (`pi@tunnel-pi.local`) |
| `SSH_CLOUDFLARE` | emergency door | an SSH hostname on your Cloudflare domain (`ssh.pi.example.com`). Empty: no door, skip 1.3 |
| `CF_TOKEN_FILE` | emergency door | where you'll save the tunnel token in 1.3 (the default is fine) |
| `WGT_SERVER` | optional | the VPS's name in `bin/wgt-ctl list`. Then `VPS_SSH` and `VPS_HOST` can stay as the example |
| `VPS_SSH` | always | how ssh reaches the VPS (`root@vps.example.com`) |
| `VPS_HOST` | always | the VPS's public name or IP, which the Pi dials |
| `PI_PORT` | always | a UDP port on the VPS nothing uses (check with `ss -ulpn` there). `443` if free |
| `TPI_ROUTING` | always | `tpi` or `wgt` |
| `CLIENT_IPS` | `tpi` | the IPv4 `Address` of each client config that should exit through the Pi (see below) |
| `CLIENT_IP6S` | `tpi`, if any | those clients' IPv6 addresses: they get blocked, since the link is IPv4 only |
| `WG_IF` | `tpi` | the VPS's WireGuard interface for your clients (`wg show interfaces`; usually `wg0`) |
| `WG_SUBNET` | `tpi` | leave empty |
| `JUMP_CODE`, `JUMP_LABEL` | `wgt` | a 2–6 character code (`a-z`, `0-9`, `-`) not already in `wgt jump list`, and a label |
| `JUMP_LAN_CODE`, `JUMP_LAN_LABEL` | `wgt`, optional | a second jump in LAN mode (2.3) |

**With `TPI_ROUTING=tpi`, make the client configs now, by hand.** tunnel-pi
makes none. Each device that should exit through the Pi needs a WireGuard
client config on your VPS, made however you normally make them; its
`Address` goes in `CLIENT_IPS`. The link step needs at least one.

```sh
scripts/tpi-ctl.sh config
```

**Check:** it ends with `settings look complete`.

## 1.2 Flash the Pi **[Hands]**

With Raspberry Pi Imager, write **Raspberry Pi OS Lite (64-bit)**, Bookworm or
later. In its settings: the hostname and user from `SSH_LAN`, and **SSH with
your public key only** (passwords off). Leave Wi-Fi out: step 3.1 handles it.
Boot the Pi at home, on ethernet.

**Check [Computer]:** `ssh SSH_LAN true` works with no password prompt.

## 1.3 The emergency door: Cloudflare **[Browser]** (skip without `SSH_CLOUDFLARE`)

In Cloudflare Zero Trust (`one.dash.cloudflare.com`). Cloudflare renames these
menus now and then; follow the nearest match.

1. **Networks → Tunnels → Create a tunnel**, type **Cloudflared**, name
   `tunnel-pi`. The install page shows a command with a long **token** in it.
   Save the token alone to `CF_TOKEN_FILE` **[Computer]**:

   ```sh
   mkdir -p ~/.config/tunnel-pi
   install -m 600 /dev/stdin ~/.config/tunnel-pi/cloudflared.token   # paste the token, then Ctrl-D
   ```

   Don't run the dashboard's install command: 1.4 does that.
2. On the tunnel's **Public Hostname** tab (or **Published application
   routes**): add `SSH_CLOUDFLARE`, type `SSH`, URL `localhost:22`.
3. **Access → Applications → Add an application → Self-hosted**, for
   `SSH_CLOUDFLARE`, with one **Allow** policy: *Emails* = your email.

**Check:** `scripts/tpi-ctl.sh config` no longer prints a note about
`CF_TOKEN_FILE`.

## 1.4 Set up the Pi's base **[Computer]**

```sh
scripts/tpi-ctl.sh base          # updates, automatic security updates, watchdog, cloudflared
ssh SSH_LAN sudo reboot          # once, so the hardware watchdog starts
scripts/tpi-ctl.sh ssh-config    # emergency door only: add what it prints to ~/.ssh/config
```

**Check:** `base` ends with `base done`, and after the reboot `ssh SSH_LAN true`
works. With the emergency door: the dashboard shows the tunnel **Healthy**, and
`ssh SSH_CLOUDFLARE` opens a browser login once **[Browser]**, then a shell.

---

# Phase 2: WireGuard

The Pi stays at home for this phase.

## 2.1 The link and the exit **[Computer]**

```sh
TP_DRY_RUN=1 scripts/tpi-ctl.sh all    # see what it will do
scripts/tpi-ctl.sh all                 # asks before it changes the VPS
```

This builds the `wgpi` link at both ends (swapping the keys for you), then the
exit: with `tpi`, the routing and kill switch for `CLIENT_IPS` on the VPS and a
DNS resolver on the Pi; with `wgt`, a jump server on the Pi, added to wgt as
jump `JUMP_CODE`. Reference lists the steps one by one.

**Check:** it ends with `all checks passed`. With `wgt`, the `wgt jump add`
output also said `jump 'JUMP_CODE' works: ip=<your home IP>`.

## 2.2 Device configs

**`TPI_ROUTING=tpi`:** nothing to do here. Your client configs from 1.1 already
go through the Pi.

**`TPI_ROUTING=wgt` [Computer]:**

```sh
scripts/tpi-ctl.sh client phone          # makes the config JUMP_CODE.phone
scripts/tpi-ctl.sh client laptop lan     # with JUMP_LAN_CODE: JUMP_LAN_CODE.laptop (LAN mode, 2.3)
```

With `WGT_SERVER`, the config lands in `~/wg-tunnels/devices/<device>/config/`
and the device keeps its fleet-wide number; show it as a QR code with
`bin/wgt-ctl WGT_SERVER client show <code>.<device> --qr`. Without it, run
`wgt client show <code>.<device> --qr` on the VPS.

These configs are made with `--warp off`: a `warp.` config would put
Cloudflare WARP inside the jump, and sites would see a Cloudflare IP instead of
the friend's.

**Check [client]:** with the new config on, `curl https://ifconfig.me` shows
your home IP (the Pi's network, for now).

## 2.3 Optional: LAN mode

By default the Pi is **isolated**: clients get the friend's internet and
nothing else. **LAN mode** also lets them reach devices on the friend's LAN by
IP: a NAS, a printer, the router's page. **Ask the friend first.**

It's one way (LAN devices can't reach your clients, and see every connection
as from the Pi), by IP only (no AirPlay or network discovery, and names like
`nas.lan` don't resolve), and clients don't get an address on that LAN.

```sh
scripts/tpi-ctl.sh lan-mode lan          # on
scripts/tpi-ctl.sh lan-mode isolated     # off
```

That switches `wgpi` (with `tpi`) or `wgjump` (with `wgt`); a third word picks
another, such as `wgjumplan`. Every client of that interface gets the same
mode. To choose per device with `wgt`, keep `wgjump` isolated and set
`JUMP_LAN_CODE`: `tpi-ctl.sh exit` adds a second jump, `wgjumplan`, in LAN
mode, and each device picks by config (`home.phone` isolated,
`homelan.laptop` LAN).

"The LAN" is the subnet the Pi is on, so at home it's yours; the Pi follows
when it moves. It must not overlap the client's own network (see
Troubleshooting).

**Check [Pi]:** `journalctl -u wg-quick@wgpi -n 20` (or `@wgjump`) shows
`tunnel-pi-exit: ... LAN 192.168.1.50/24 reachable` (the Pi's address on that
LAN).

---

# Phase 3: finish, prove, drop off

## 3.1 Ethernet or Wi-Fi at the friend's **[Computer]**

The Pi uses ethernet when it's plugged in, else any saved Wi-Fi in range, and
moves between them on its own. Choose now:

```sh
scripts/tpi-ctl.sh wifi                              # what's saved, and what the Pi is using
scripts/tpi-ctl.sh wifi remove all                   # ethernet only: the usual choice
scripts/tpi-ctl.sh wifi add "<friend's network>"     # or their Wi-Fi too (asks for the password)
```

Ethernet only is the most reliable, and the friend sets up nothing. Their
Wi-Fi needs their password, and helps where a cable can't reach. `remove`
won't remove the network the Pi is using right now: plug in ethernet first.

**Check:** `scripts/tpi-ctl.sh check` shows `INFO  uplink: eth0. Saved Wi-Fi: ...`
as you expect.

## 3.2 Harden

- **Storage:** power cuts wear out SD cards. Use an SSD (USB boot) if you can.
  The SD-card alternative, raspi-config's overlay file system, also throws away
  every later change, `tpi-ctl.sh` commands and updates included, unless you
  turn it off first. If you use it, turn it on last, just before drop-off.
- **Updates:** `base` turned on automatic security updates.

## 3.3 The away-from-home test **[Hands]**, then **[Computer]** and **[client]**

Put the Pi on a **phone hotspot**: like the friend's network, a NAT you don't
control. Use a travel router on the hotspot with the Pi's ethernet in it, a
USB-to-ethernet adapter tethered to the phone, or the hotspot's Wi-Fi
(`scripts/tpi-ctl.sh wifi add "<hotspot>"` while still on ethernet, then
unplug; remove it again after the tests with
`scripts/tpi-ctl.sh wifi remove "<hotspot>"`, once ethernet is back in). No
reboot is needed: the Pi follows the network change on its own.

Keep your computer and the client on another network. "The Pi's IP" below
is the hotspot's, which `tpi-ctl.sh check` prints.

| # | Test | Pass |
|---|---|---|
| 1 | **[Computer]** `scripts/tpi-ctl.sh check` | `all checks passed`; its first line says `via the link` |
| 2 | **[client]** (a Pi-exit config) `curl https://ifconfig.me` | the Pi's IP |
| 3 | **[client]** a DNS leak test (such as dnsleaktest.com) | `tpi`: only the Pi's ISP. `wgt`: Cloudflare's resolvers, reached from the Pi's IP |
| 4 | **[client]** `curl -6 -m 10 https://ifconfig.me` | fails: IPv6 is blocked |
| 5 | **[Computer]** stop the Pi's end of the link: `ssh SSH_CLOUDFLARE sudo systemctl stop wg-quick@wgpi` (without the door: `ssh -J VPS_SSH <user>@10.99.0.2 ...`, whose session then hangs: Ctrl-C). Then **[client]** `curl -m 10 https://ifconfig.me` | times out. It must **not** print the VPS's IP |
| 6 | the link still down: **[Computer]** `scripts/tpi-ctl.sh check` | emergency door only: its first line says `via SSH_CLOUDFLARE` |
| 7 | **[Computer]** `ssh SSH_CLOUDFLARE sudo systemctl start wg-quick@wgpi` (without the door: power-cycle the Pi) | `tpi-ctl.sh check` passes again |
| 8 | `tpi` only: **[VPS]** `systemctl stop wg-quick@wgpi`, **[client]** `curl -m 10 https://ifconfig.me`, **[VPS]** `systemctl start wg-quick@wgpi` | times out (the kill switch). Never the VPS's IP |
| 9 | **[client]** `ping -c2` the Pi's router (`ip route` on the Pi: after `default via`) | isolated: no reply. LAN mode: replies |
| 10 | **[client]** a config that is **not** a Pi exit: `curl https://ifconfig.me` | the same IP as before tunnel-pi |

## 3.4 Survive a power cut **[Hands]**

Unplug the Pi and plug it back in.

**Check [Computer]:** a minute later, untouched, `scripts/tpi-ctl.sh check`
passes and test 2 passes again. Drop-off is the same power-up on another
network.

## 3.5 Drop-off **[Hands]**

The friend plugs in ethernet (unless you saved their Wi-Fi) and power. That's
all.

**Check [Computer], within a few minutes:** `scripts/tpi-ctl.sh check` passes
and shows the friend's IP, and so does a Pi-exit client's
`curl https://ifconfig.me`. Tests 2–10 of 3.3 are worth repeating.

If it doesn't come up: ask the friend to check the ethernet lights, then to
unplug the power and plug it back in. Still nothing usually means their
network blocks outbound UDP to `PI_PORT`. With the emergency door you'll see it
(`ssh SSH_CLOUDFLARE` works, `check` shows no `wgpi` handshake): set
`PI_PORT="443"` (if free on the VPS) and run `scripts/tpi-ctl.sh link`.
Without the door, bring the Pi home to change it.

---

# Day to day

- **Add or remove a `tpi` client:** edit `CLIENT_IPS` (and `CLIENT_IP6S`),
  then `scripts/tpi-ctl.sh link`. A removed client's rules are removed too.
- **Add a `wgt` device:** `scripts/tpi-ctl.sh client <device>` (2.2).
- **After `wgt restore` or `wgt-ctl move` on the VPS:** wgt's backups include
  `wgpi`, but don't start it: run `scripts/tpi-ctl.sh link`.
- **Your VPS's IP changes:** the Pi looks up `VPS_HOST` only when the link
  starts: `ssh SSH_CLOUDFLARE sudo systemctl restart wg-quick@wgpi`, or have
  the friend power-cycle the Pi.
- **Look around on the Pi:** `ssh SSH_LAN` at home, `ssh SSH_CLOUDFLARE`, or
  through the VPS: `ssh -J VPS_SSH <user>@10.99.0.2`.

## Undo **[Computer]**

With `TPI_ROUTING=wgt`, first remove each device's config (wgt won't remove a
jump that configs use): `bin/wgt-ctl WGT_SERVER client remove <code>.<device>`,
or without `WGT_SERVER`, `wgt client remove <code>.<device>` on the VPS. Then:

```sh
scripts/tpi-ctl.sh undo          # the VPS's tunnel-pi parts, then the Pi's WireGuard parts
scripts/tpi-ctl.sh undo all      # the same, plus cloudflared on the Pi
```

Undo removes the link, so it runs over `SSH_LAN` or `SSH_CLOUDFLARE`, not
through it. After `undo all`, delete the tunnel and the Access application in
the dashboard **[Browser]**.

**Check [VPS]:** `ip rule` shows nothing at `900` or `901`, `wgt jump list` no
longer shows the code(s), and clients are back on their usual routes.

## Troubleshooting

| Symptom | Look at |
|---|---|
| `check` says `via the link` at home | `SSH_LAN` didn't answer: is the Pi on, and does `ssh SSH_LAN true` work without a prompt? |
| `check` says `via SSH_CLOUDFLARE` | the link is down; you're in through the emergency door. The rows below say why |
| the Pi can't be reached at all | with the door: is the tunnel Healthy in the dashboard, your email in the Access policy, the `ssh-config` entry in `~/.ssh/config`? Without it: the friend power-cycles the Pi |
| `tpi-ctl.sh` waits at "This changes the VPS ... Continue?" | run it in a terminal, or `TP_YES=1` once the person has agreed |
| `10.99.0.0/30 is already used` or `port ... in use` | pick another `PI_PORT`, or free those addresses on the VPS |
| FAIL `wgpi has no recent handshake` | **[VPS]** is `PI_PORT/udp` open (`ss -ulpn`, ufw, the provider's firewall)? **[Pi]** does `VPS_HOST` resolve? Then `scripts/tpi-ctl.sh link` again |
| **[Pi]** `wg-quick@wgpi` keeps restarting | `journalctl -u wg-quick@wgpi -n 20`: `no default route yet` is no network; a failed lookup is `VPS_HOST` |
| `wgt jump add` fails | **[VPS]** `ping 10.99.0.2` (the link first), `wg show px-JUMP_CODE`; **[Pi]** `sudo wg show wgjump`. Then `TP_RENEW=1 scripts/tpi-ctl.sh exit` (new jump keys) |
| `the Pi's jump server exists but the VPS has no jump` | `TP_RENEW=1 scripts/tpi-ctl.sh exit` |
| `tpi`: handshake, but no internet on the client | **[VPS]** `ip rule` (900 above the rest?), `ip route show table 260`; **[Pi]** `sudo iptables -t nat -L -v`, `journalctl -u wg-quick@wgpi` (its `tunnel-pi-exit:` line names the uplink) |
| `tpi`: sites load, names don't resolve | **[Pi]** `systemctl status unbound`; **[VPS]** `iptables -t nat -L PREROUTING -v` |
| large pages hang | MTU: a client config's `MTU` above 1280 needs lowering |
| a client shows the VPS's IP | `tpi`: is it in `CLIENT_IPS`? `systemctl status tunnel-pi-killswitch`. `wgt`: is the config named `<code>.<device>`? |
| traffic stops after the Pi changes networks | **[Pi]** `journalctl -t tunnel-pi-exit -n 20` shows each move; if none, reboot the Pi |
| LAN mode: some LAN addresses don't answer | the friend's LAN overlaps the client's own network (a café on `192.168.1.0/24` too), a subnet of `WG_IF`, or `10.98.0.0/29`/`10.99.0.0/30`. Try the client from another network |

---

# Reference

## Commands

All from **[Computer]**, as `scripts/tpi-ctl.sh <command>`:

| Command | Does |
|---|---|
| `config` | prints the settings and checks them |
| `ssh-config` | the `~/.ssh/config` entry for `SSH_CLOUDFLARE` |
| `base` | the Pi: package updates, automatic security updates, the hardware watchdog, and with the door, cloudflared |
| `link` | the Pi: WireGuard, IP forwarding, a retry at boot, its key, and a hook that moves the exit rules when the network changes. The VPS: its key, its end of `wgpi`, `PI_PORT/udp` in ufw if active, and with `tpi` the routing and kill switch for `CLIENT_IPS`. Then the Pi's end |
| `exit` | `tpi`: the DNS resolver (unbound) on the Pi. `wgt`: the jump server(s) on the Pi, piped into `wgt jump add` on the VPS |
| `all` | `link`, `exit`, `check` |
| `client <device> [lan]` | `wgt`: a config for the device (`lan`: through the LAN-mode jump) |
| `lan-mode isolated\|lan [<interface>]` | switch LAN mode (default `wgpi` or `wgjump`) |
| `wifi`, `wifi add <name>`, `wifi remove <name>\|all` | the Pi's saved Wi-Fi |
| `check` | PASS/FAIL on both ends |
| `undo [all]` | remove it all (`all`: cloudflared too) |

Environment variables:

| Variable | Does |
|---|---|
| `TP_DRY_RUN=1` | shows what would run, and changes nothing |
| `TP_YES=1` | answers yes to "This changes the VPS ... Continue?" |
| `TP_RENEW=1` | with `exit`: makes new jump keys |
| `TP_CONF=<file>` | reads another settings file |

## How tpi-ctl.sh reaches the machines

**The Pi:** the first way that answers, and each run says which:

1. `SSH_LAN`, on your home network.
2. Through the VPS over the link: `ssh -J VPS_SSH <user>@10.99.0.2`.
3. `SSH_CLOUDFLARE`, the emergency door.

**The VPS:** `VPS_SSH`. With `WGT_SERVER`, through `wgt-ctl` instead: its
server list and host-key checks, and every wgt command as
`wgt-ctl WGT_SERVER ...`, so configs reach `~/wg-tunnels`. That needs
`tunnel-pi/` inside the wireguard-tools checkout, next to `bin/wgt-ctl`.

**The scripts:**

| Script | Runs |
|---|---|
| `scripts/tpi-ctl.sh` | on your computer: the one you run |
| `scripts/pi.sh` | on the Pi, as root, sent by `tpi-ctl.sh` (the top says how to run it by hand) |
| `scripts/vps.sh` | on the VPS, as root, sent by `tpi-ctl.sh` (likewise) |
| `scripts/tunnel-pi-exit` | installed on the Pi: the exit's firewall rules |

**Secrets** never go on a command line. The token and the Wi-Fi password
travel inside ssh into root-only files, and the jump file goes from the Pi to
the VPS through a pipe.

## What the scripts change

**On the Pi:**

| File or setting | When | What it is |
|---|---|---|
| `/etc/wireguard/wgpi.*` | `link` | the link to the VPS: config and keys |
| `/etc/wireguard/wgjump*`, `wgjumplan*` | `wgt` | the jump server(s), config and keys |
| `/etc/wireguard/jump-for-vps*.conf` | `wgt`, briefly | the jump file for the VPS, deleted once wgt has it |
| `/usr/local/sbin/tunnel-pi-exit` | `link` | the exit's firewall rules |
| `/etc/sysctl.d/99-tunnel-pi.conf` | `link` | IP forwarding on |
| `/etc/systemd/system/wg-quick@.service.d/tunnel-pi-retry.conf` | `link` | `wg-quick` keeps retrying at boot |
| `/etc/NetworkManager/dispatcher.d/90-tunnel-pi` | `link` | moves the exit rules when the network changes |
| `/etc/unbound/unbound.conf.d/tunnel-pi.conf` | `tpi` | the DNS resolver for Pi-exit clients |
| `RuntimeWatchdogSec=15` in `/etc/systemd/system.conf` | `base` | the hardware watchdog |
| `unattended-upgrades` | `base` | automatic security updates |
| Cloudflare's apt repository, `/etc/systemd/system/tunnel-pi-cloudflared.service` | `base`, with the door | cloudflared and its service |
| `/etc/cloudflared/tunnel-pi.env` | `base`, with the door | the tunnel token (mode 600) |
| `/etc/NetworkManager/system-connections/tunnel-pi-*.nmconnection` | `wifi add` | a saved Wi-Fi network, with its password (mode 600) |

**On the VPS:**

| File or setting | When | What it is |
|---|---|---|
| `/etc/wireguard/wgpi.*` | `link` | the link to the Pi: config and keys (with `tpi`, also the client routing) |
| a ufw rule for `PI_PORT/udp` | `link`, if ufw is active | lets the Pi dial in |
| `/usr/local/sbin/tunnel-pi-killswitch`, `/etc/systemd/system/tunnel-pi-killswitch.service` | `tpi` | the kill switch |
| wgt's own jumps and client configs | `wgt` | made by `wgt jump add` and `wgt client add` |

Nothing changes `WG_IF`'s configuration, existing client configs, or wgt's
files.

## Fixed values

Chosen to avoid wgt's and common setups'; `vps.sh` stops if the VPS already
uses the link's addresses or `PI_PORT`.

| Value | What it is |
|---|---|
| `10.99.0.0/30` | the link: VPS `10.99.0.1`, Pi `10.99.0.2` |
| `10.98.0.0/30`, UDP `51822` | `wgt`: the jump server (Pi `10.98.0.1`, VPS `10.98.0.2`); the LAN-mode jump: `10.98.0.4/30`, `51823` |
| table `260`, rule `900` | `tpi`: the table that goes to the Pi, and the rule that sends a listed client to it |
| rule `901` | `tpi`: the kill switch, which drops a listed client's traffic when 900 can't apply (wgt uses `100`, `200`–`250`, `9300`–`10000`) |
| `wgpi`, `wgjump`, `wgjumplan` | the link and the jump servers |

## Why not carry WireGuard over Cloudflare?

WireGuard is UDP only, and a Cloudflare Tunnel carries HTTP, WebSocket and
TCP. Wrapping one in the other needs a helper on every client, which phones
can't run. So client traffic takes the link through your VPS, and Cloudflare
carries only your SSH, well inside its free plan and rules.

## Limits and care

- **Speed:** your downloads are the friend's uploads, so their upload speed
  caps yours, and every request takes the longer path client → VPS → Pi.
- **The friend's IP changes:** fine. The Pi dials out again.
- **Other things that give you away:** IP, DNS and IPv6 are covered. WebRTC,
  the browser's time zone and language, location services and account history
  aren't; strict services (banks, streaming) look at them.
- **The friend:** to their ISP, this traffic is their household's. Let only
  clients you trust use it, and tell the friend what the box does.
