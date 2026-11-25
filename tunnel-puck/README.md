# tunnel-puck: a small computer as a residential exit

Put a **puck**, a small single-board computer (SBC) like a Raspberry Pi, on a
friend's home network, and chosen WireGuard clients of your VPS reach the
internet **from the friend's home IP**. The friend only plugs in power and an
ethernet cable: no router settings, no open ports.

```
client ──WireGuard──▶ your VPS ──WireGuard link──▶ puck at the friend's ──▶ internet
                                  ▲ the puck dials out and keeps it open, so the friend's NAT doesn't matter
```

Your clients still connect to your VPS. The ones you choose get a config of
their own for it (with `tpk`, their address is listed instead), and the VPS
sends their traffic down a link the puck keeps open, and the puck sends it out
under the friend's IP. You build and test everything at home; at drop-off the
puck only needs power. One puck can serve several VPSes, each over its own link
([One puck, several VPSes](#one-puck-several-vpses)).

**Where each tunnel starts and ends, and who sees the traffic.** With `wgt`:

```
client ══[1]══▶ VPS wg0 ─ plain ─▶ VPS ══[2 inside 3]══▶ puck wgpk0 → wgrtun0 ─ plain ─▶ friend's ISP ──▶ sites
  [1] the client's config: client ↔ VPS
  [2] the jump (rtun): VPS px-<code> ↔ puck wgrtun<n>
  [3] the link: VPS wgpk-<puck> ↔ puck wgpk<n>, dialled by the puck
```

| Where | Sees the traffic in the clear? |
|---|---|
| the client's own network, its ISP | no: only [1], encrypted, to the VPS |
| **the VPS** | **yes**: it unwraps [1] and wraps the traffic again for the puck, as any VPN server would |
| between the VPS and the puck, the friend's router | no: [2] inside [3], encrypted twice |
| **the puck** | **yes**: it unwraps [3] then [2], and sends the traffic out |
| **the friend's ISP, and the sites** | **yes**, as for any traffic from that home (HTTPS still hides the content) |

With `tpk` there's no [2]: the VPS routes the listed clients straight into the
link, so it's [1], then [3] alone. Who sees what is the same.

To keep the traffic from the VPS too, the client would have to wrap it
itself, a puck tunnel inside its VPS tunnel, as
[wireguard-tunnels](https://github.com/cgmckeever/wireguard-tunnels)' warped
tunnels do with WARP. tunnel-puck doesn't do that. Phones would need
[sing-box](https://sing-box.sagernet.org) for it, and a Mac wireguard-tunnels,
sing-box or two `wg-quick` configs.

**Status:** tested only on a Raspberry Pi so far: a Pi 2 v1.1 (32-bit
Raspberry Pi OS Bookworm), with `TPK_ROUTING=wgt` against a test VPS. Phases 1
and 2 ran from a fresh image, and an iPhone on cellular came out with the
puck's home IP. Unplugged and moved behind a travel router, the puck came back
on its own and was reached through the link. The same puck then served a
second VPS alongside the first, each with its own link and jump, and the
iPhone went through both. Still untested: the rest of phase 3 (fail closed, a
DNS leak test), `tpk` routing and the Cloudflare door.
Run it first on a VPS nobody else depends on.

## Installation success

1. A chosen client's public IP (`curl https://ifconfig.me`) is the residential
   (or other) IP of the network where the tunnel-puck sits.
2. Its DNS lookups also leave from that IP.
3. When the link is down, that client gets no connection at all. It never
   falls back to the VPS's IP.
4. Nothing from the tunnel reaches devices on the friend's LAN, unless you
   turn on [LAN mode (2.3)](#23-optional-lan-mode).
5. The VPS's other clients, the ones not sent to the puck, still go out through
   the VPS as before.
6. You can reach the puck remotely.

Phase 3 tests each of these.

## Choose your routing

Two choices: the routing for each VPS the puck serves (in that VPS's settings
file), and the emergency door for the puck (in its own).

**1. Routing: how your VPS sends clients into the puck.** Pick the row that
matches your VPS:

| Your VPS | Set | What you get |
|---|---|---|
| any WireGuard server | `TPK_ROUTING="tpk"`, with `VPS_SSH` and `VPS_HOST` | tunnel-puck's own rules on the VPS. You make the clients' configs yourself on the VPS and list their addresses in `CLIENT_IPS` ([2.2](#22-device-configs)) |
| runs **wgt** (the wireguard-tools in this repository) | `TPK_ROUTING="wgt"`, with `WGT_SERVER` (its name in `bin/wgt-ctl list`) | the puck becomes a wgt **jump**: an extra way out on a wgt server, which a client config named `<code>.<device>` uses instead of the VPS's own. Everything on the VPS goes through `wgt-ctl`, and `tpk-ctl.sh client` makes each device its config, delivered to `~/wg-tunnels` |

With `wgt`, [2.1](#21-serve-a-vps-computer) fills these in for you.

**2. The emergency door (`SSH_CLOUDFLARE`): recommended.** Normally you reach
the puck through a VPS, over its link (with several VPSes, any of them). That
fails exactly when the links are down (a blocked port, a changed VPS IP, a bad
change). A Cloudflare Tunnel gives SSH
to the puck that doesn't need the link. It costs a domain on Cloudflare (about
$10–15 a year; the rest is free) and one extra step,
[1.3](#13-the-emergency-door-cloudflare-browser-skip-without-ssh_cloudflare).
Without it, a dead link means the friend power-cycles the puck, or you take it
home. If you skip it, set `PUCK_PORT="443"` in each VPS's settings file when
you reach 2.1: networks rarely block it.

## What you need

- **A puck:** a small single-board computer with wired ethernet, its storage
  (an SSD, or an SD card) and a power supply. What tunnel-puck needs from it:
  - **Debian Bookworm or later**, or a Debian-based OS like Raspberry Pi OS,
    with systemd and **NetworkManager** (it moves the exit when the network
    changes, and keeps the Wi-Fi list);
  - `apt`, and packages it installs as it goes: WireGuard, `iptables`,
    `unbound`, `unattended-upgrades`, and with the emergency door,
    `cloudflared`;
  - ssh with your public key, and `sudo` without a password for its user;
  - a hardware watchdog (`/dev/watchdog`) is used when there is one.

  **Tested only on a Raspberry Pi**, and the steps below use one: Raspberry Pi
  Imager and Raspberry Pi OS Lite (64-bit), Bookworm or later. Another board
  that meets the list above should work, but hasn't been tried; flash its
  Debian image your usual way instead of 1.2.

  **Which Raspberry Pi:** a 3 or later with ethernet (3B, 3B+, 4 or 5). A 4 or
  5 is fastest; on a 3, its 100–300 Mbit/s ethernet is the ceiling, though the
  friend's upload speed usually is anyway. A **Pi 2** works for testing, and
  may be enough for light use: phases 1 and 2 have run on an original one
  (v1.1). It has no Wi-Fi and boots from an SD card. The v1.1 needs the 32-bit
  Lite image; check that `cloudflared` installs on it before relying on the
  emergency door. Measured on it (Mbit/s, each way):

  | Path | On the home router | Behind a travel router |
  |---|---|---|
  | the link alone (one layer of WireGuard) | about 87 | 57–68 |
  | a `wgt` client's path (the jump inside the link: two layers) | 65–68 | 42–66 |
- **A Linux VPS** running the WireGuard server your clients use, with
  `wg-quick`, `iptables` and systemd (Debian and Ubuntu have all three), and
  root ssh (or passwordless `sudo`). With `TPK_ROUTING=wgt`, wgt on it.
- **Your computer** (macOS or Linux) with `ssh`, and this folder.
- **For the emergency door:** a domain whose DNS is on Cloudflare, a Cloudflare
  Zero Trust account (free; signing up may ask for a card), and `cloudflared`
  on your computer (macOS: `brew install cloudflared`).

## How to read this

- **One command does the work:** `scripts/tpk-ctl.sh`, run on your computer in
  this folder. It ssh-es to the puck and the VPS and runs `puck.sh` and `vps.sh`
  there. `scripts/tpk-ctl.sh` with no arguments lists its commands.
- **Each puck has a name, `<puck>`:** its settings file is
  `configs/<puck>.conf`, and each VPS it serves (`<vps>`) has one more,
  `configs/<puck>.<vps>.conf`. Commands take the name first,
  `scripts/tpk-ctl.sh mom test`, or `mom.dengar` for one VPS. Leave it out, as
  the steps below do, and `tpk-ctl.sh` uses the only puck (and VPS) there is,
  or with several asks which (`TPK_PUCK=<puck>` and `TPK_VPS=<vps>` set the
  defaults). It always says which it chose. More than one: see
  [More than one tunnel-puck endpoint](#more-than-one-tunnel-puck-endpoint) and
  [One puck, several VPSes](#one-puck-several-vpses).
- **A setting's name stands for its value:** `ssh SSH_LAN` means
  `ssh pi@tunnel-puck.local`.
- **`bin/wgt-ctl`** is the repository's: from this folder, `../bin/wgt-ctl`.
- **The examples** use a puck called `mom` on the VPS `dengar`, a second VPS
  `ig88`, and a second puck `home`.
- **Every step ends with a Check.** Don't go on until it passes.
- **Safe to repeat:** every command can run again. Files change only when they
  must, and keys are kept. `TPK_DRY_RUN=1 scripts/tpk-ctl.sh <command>` shows
  what would run without changing anything.
- **Markers** say where a step happens:

| Marker | Where |
|---|---|
| **[Computer]** | your machine, in this folder |
| **[Puck]** | a shell on the puck |
| **[VPS]** | a root shell on the VPS |
| **[client]** | a device connected to the VPS |
| **[Browser]** | the Cloudflare dashboard |
| **[Hands]** | something physical: flashing, plugging in, a phone hotspot |

### For an AI agent following this

- **Ask before touching the VPS.** `vps add`, `vps remove`, `link`, `all`,
  `client`, `disable`, `enable`, `undo`, and `exit` with `TPK_ROUTING=wgt`
  change it (`status` only reads it), and `test`
  with `wgt` brings its jump up for a moment. Other people's traffic may run
  through it. Get the person's go-ahead for that server first, read-only
  commands included, and suggest a test server. `tpk-ctl.sh` also asks on
  the terminal; once the person has agreed for this VPS, `TPK_YES=1` answers
  for you. Reaching the puck through the VPS changes nothing there, but tell the
  person it connects. Follow any rules the person has about which servers you
  may use.
- **Some steps need a person:** everything marked **[Browser]** or
  **[Hands]**, the browser login on the first `ssh SSH_CLOUDFLARE`, making
  client configs with `TPK_ROUTING=tpk`, filling in a `tpk` VPS's settings
  (`vps add` stops for it), and typing a Wi-Fi password. Stop, say exactly
  what to do, and wait.
- **Name the puck** on every command (`scripts/tpk-ctl.sh home all`) once
  there's more than one, and the VPS too (`home.dengar`) once a puck serves
  more than one: with no terminal, `tpk-ctl.sh` can't ask.
- **Dry run first:** `TPK_DRY_RUN=1 scripts/tpk-ctl.sh all`.
- **Secrets stay put.** Never print a `*.key` file, a `PrivateKey` line, a jump
  file, the Cloudflare token or a Wi-Fi password. The scripts never do.
- **A FAIL means stop.** Report the command, its output and the matching
  Troubleshooting row. Don't improvise a fix on the VPS.

---

# Phase 1: prepare the puck

## 1.1 Fill in the settings **[Computer]**

Each puck gets a settings file named for it, in `configs/` (git ignores them).
The name is up to 10 characters of `a-z`, `0-9` and `-`, starting with a
letter. `new` asks for it and makes the file from the template, and offers a
Wi-Fi list too ([3.1](#31-ethernet-or-wi-fi-at-the-friends-computer)). This
file holds the puck's own settings; each VPS it serves gets its own file in
[2.1](#21-serve-a-vps-computer).

```sh
scripts/tpk-ctl.sh new           # or: scripts/tpk-ctl.sh new mom [--wifi]
```

Fill it in (each line has a comment), then check it:

| Setting | When | What to put |
|---|---|---|
| `SSH_LAN` | always | `user@hostname` for the puck on your home network. You choose both in [1.2](#12-flash-the-puck-hands) (`pi@tunnel-puck.local`) |
| `SSH_CLOUDFLARE` | emergency door | an SSH hostname on your Cloudflare domain (`ssh.pi.example.com`). Empty: no door, skip [1.3](#13-the-emergency-door-cloudflare-browser-skip-without-ssh_cloudflare) |
| `CF_TOKEN_FILE` | emergency door | where you'll save the tunnel token in [1.3](#13-the-emergency-door-cloudflare-browser-skip-without-ssh_cloudflare) (the default is fine) |

```sh
scripts/tpk-ctl.sh config
```

**Check:** it ends with `settings look complete`.

## 1.2 Flash the puck **[Hands]**

On a Raspberry Pi: with Raspberry Pi Imager, write the OS from
[What you need](#what-you-need). (Another board: its Debian image, set up the
same way.) In Imager's settings: the hostname and user from `SSH_LAN`, and **SSH with your public
key only** (passwords off). Leave Wi-Fi out: step
[3.1](#31-ethernet-or-wi-fi-at-the-friends-computer) handles it. Boot the puck at
home, on ethernet.

**Check [Computer]:** `ssh SSH_LAN true` works with no password prompt.

## 1.3 The emergency door: Cloudflare **[Browser]** (skip without `SSH_CLOUDFLARE`)

In Cloudflare Zero Trust (`one.dash.cloudflare.com`). Cloudflare renames these
menus now and then; follow the nearest match.

1. **Networks → Tunnels → Create a tunnel**, type **Cloudflared**, name
   `tunnel-puck`. The install page shows a command with a long **token** in it.
   Save the token alone to `CF_TOKEN_FILE` **[Computer]**:

   ```sh
   mkdir -p ~/.config/tunnel-puck
   install -m 600 /dev/stdin ~/.config/tunnel-puck/cloudflared.token   # paste the token, then Ctrl-D
   ```

   Don't run the dashboard's install command: [1.4](#14-set-up-the-puck-init-computer) does that.
2. On the tunnel's **Public Hostname** tab (or **Published application
   routes**): add `SSH_CLOUDFLARE`, type `SSH`, URL `localhost:22`.
3. **Access → Applications → Add an application → Self-hosted**, for
   `SSH_CLOUDFLARE`, with one **Allow** policy: *Emails* = your email.

**Check:** `scripts/tpk-ctl.sh config` no longer prints a note about
`CF_TOKEN_FILE`.

## 1.4 Set up the puck: init **[Computer]**

```sh
scripts/tpk-ctl.sh init          # updates, automatic security updates, watchdog, cloudflared
scripts/tpk-ctl.sh reboot        # once, so the hardware watchdog starts
scripts/tpk-ctl.sh ssh-config install   # emergency door only: so your own ssh reaches it
```

`tpk-ctl.sh` itself needs no ssh setup for the door: through Cloudflare it
passes `cloudflared` to ssh itself. `ssh-config install` is for your own
`ssh SSH_CLOUDFLARE`: it writes every puck's door to
`~/.config/tunnel-puck/ssh_config` (rewritten each time) and adds one
`Include` line for it at the top of `~/.ssh/config`, after a backup; the rest
of that file is left alone. (`ssh-config` alone prints the entry instead.)

**Check:** `init` ends with `init done`, and `reboot` ends with `<puck> is back`. With the emergency door: the dashboard shows the tunnel
**Healthy**, and `ssh SSH_CLOUDFLARE` opens a browser login once **[Browser]**,
then a shell.

---

# Phase 2: WireGuard

The puck stays at home for this phase.

## 2.1 Serve a VPS **[Computer]**

```sh
scripts/tpk-ctl.sh vps add dengar      # asks before it changes the VPS
```

`vps add` makes the VPS's settings file, `configs/<puck>.<vps>.conf`, from
`configs/puck.vps.conf.example`:

- **A server in `../bin/wgt-ctl list`** (here `dengar`) gets `TPK_ROUTING="wgt"`
  and `WGT_SERVER` filled in, and `vps add` carries on at once.
- **Any other name** gets a `tpk` file and stops: fill in the `tpk` settings
  below, then run `vps add` again. (For a wgt server under another name, set
  `TPK_ROUTING="wgt"` and `WGT_SERVER` instead.)

Then:

- **It builds the link** at both ends, swapping the keys for you: `wgpk-<puck>`
  on the VPS, `wgpk<n>` on the puck.
- **Then the exit.** With `tpk`: the routing and kill switch for `CLIENT_IPS`
  on the VPS, and a DNS resolver on the puck. With `wgt`: a **reverse tunnel
  (rtun)** on the puck, `wgrtun<n>`, which the VPS reaches in through, added
  to wgt as jump `JUMP_CODE`.
- **Last, `test`** for that VPS. `link`, `exit` and `test` also run on their
  own; [Commands](#commands) lists what each does.
- **If a step fails,** fix what it says and run `vps add` again: it picks up
  the settings file it made, and every step is safe to repeat.
- **For a second VPS,** run `vps add` again with its name
  ([One puck, several VPSes](#one-puck-several-vpses)).

The first `link` picks the link's subnet (`TPK_LINK`) and, if empty,
`PUCK_PORT`, from what's free on the VPS, and saves both in the VPS's settings
file. The subnet also names the puck's interfaces for this VPS: `n` is its
place in `10.99.0.0/24`, counting from 0 (`10.99.0.0/30` is `wgpk0` and
`wgrtun0`, `10.99.0.4/30` is `wgpk1` and `wgrtun1`; [Fixed values](#fixed-values)).
It's kept apart from the puck's links to other VPSes, and from other pucks' on
the same VPS, so a puck's first link isn't always `wgpk0`: `scripts/tpk-ctl.sh vps`
lists them.

The VPS's settings (each line of the file has a comment):

| Setting | When | What to put |
|---|---|---|
| `TPK_ROUTING` | always | `tpk` or `wgt` |
| `PUCK_PORT` | optional | the link's UDP port on the VPS. Empty: `link` picks a free one and saves it. `443`, if free, is rarely filtered |
| `TPK_LINK` | leave empty | the link's subnet: `link` picks one and saves it. Never change it once set |
| `VPS_SSH` | `tpk` | how ssh reaches the VPS (`root@vps.example.com`) |
| `VPS_HOST` | `tpk` | the VPS's public name or IP, which the puck dials |
| `CLIENT_IPS` | `tpk` | the IPv4 `Address` of each client config that should exit through the puck. Can wait until [2.2](#22-device-configs) |
| `CLIENT_IP6S` | `tpk`, if any | those clients' IPv6 addresses: they get blocked, since the link is IPv4 only |
| `WG_IF` | `tpk` | the VPS's WireGuard interface for your clients (`wg show interfaces`; usually `wg0`) |
| `WG_SUBNET` | `tpk` | leave empty (it means every subnet `WG_IF` is on) |
| `WGT_SERVER` | `wgt` | the VPS's name in `../bin/wgt-ctl list`; its ssh target and address come from there. Empty: the file's `<vps>` if wgt-ctl lists it, else `tpk-ctl.sh` asks (or adds a new server) and saves it |
| `JUMP_CODE`, `JUMP_LABEL` | `wgt`, optional | the puck's jump on the VPS (wgt's codes are 2–6 of `a-z`, `0-9`, `-`) and a label for it. Empty: the puck's name, the code shortened to 6 (`grandma`: `grandm`); `config` shows it. A code the VPS already uses for something else is refused |
| `JUMP_LAN` | `wgt`, optional | `yes`: a second jump in LAN mode ([2.3](#23-optional-lan-mode)), coded as the jump's with `-l` (`mom`: `mom-l`; `grandma`: `gran-l`) |
| `JUMP_LAN_CODE`, `JUMP_LAN_LABEL` | `wgt`, optional | name the LAN jump yourself (this also turns it on); the label defaults to the jump's with " (LAN)" |

Among `vps add`'s output with `wgt`, `wgt jump add` also tests the new jump and prints
`jump '<code>' works: ip=... warp=on`. That IP is **Cloudflare's, not your
home's**: wgt gives every jump its own WARP account (it registers one on the
VPS) and tests through it, for `warp.` configs. tunnel-puck's configs are made
without WARP, so they go out with your home IP; `tpk-ctl.sh test`'s end-to-end
row is where you see that.

**Check:** `vps add` ends with `all tests passed` (later, `scripts/tpk-ctl.sh
test` runs the same for every VPS), with handshakes on `wgpk<n>` and
`wgpk-<puck>` and, with `wgt`, on `wgrtun<n>`. With `wgt` its
last row tests a client's way out end to end, with no device needed: wgt brings
the jump up and traces through it without WARP, and the IP it sees must be the
puck's own (`PASS  clients exit from the puck's network: <IP>`).


## 2.2 Device configs

Each device that should exit through the puck needs a WireGuard config. How you
make it depends on your routing.

### With `TPK_ROUTING=tpk`: by hand **[VPS]**, then **[Computer]**

tunnel-puck makes no client configs: you do, on your VPS, however you normally
make them. Then send them through the puck:

1. Make a client config for each device on the VPS (or pick existing ones).
2. Put each config's IPv4 `Address` in `CLIENT_IPS` (and any IPv6 address in
   `CLIENT_IP6S`) in that VPS's settings file, `configs/<puck>.<vps>.conf`.
3. Run `scripts/tpk-ctl.sh link` again (`<puck>.<vps> link` with several
   VPSes): it adds the routing and the kill switch for them.

To add or remove a device later, edit `CLIENT_IPS` and run `link` again.

### With `TPK_ROUTING=wgt`: tpk-ctl.sh makes them **[Computer]**

The puck is a wgt jump on the VPS, coded `JUMP_CODE` (`<code>` below), which is
the puck's name unless you set it. Each device gets a config named
`<code>.<device>` on the VPS, delivered to your computer as
`<server>.<code>.<device>.conf` (`<server>` is `WGT_SERVER`). For a puck
called `mom` on the server `dengar`:

```sh
scripts/tpk-ctl.sh mom client iphone          # mom.iphone on dengar
scripts/tpk-ctl.sh mom client laptop lan      # with JUMP_LAN=yes: mom-l.laptop, LAN mode (2.3)
scripts/tpk-ctl.sh mom.ig88 client iphone     # when mom serves several VPSes: on that one
```

The config lands locally in
`~/wg-tunnels/devices/iphone/config/dengar.mom.iphone.conf`, and the device
keeps its wgt device number (its static address). To show it as a QR code:
`../bin/wgt-ctl dengar client show mom.iphone --qr`. On Android, the WireGuard
app takes tunnel names of up to 15 characters, so shorten a longer one
(`dengar.mom.iphone` is 17) when you import it; wgt warns when that applies.

These configs are made with `--warp off`: a `warp.` config would put
Cloudflare WARP inside the jump, and sites would see a Cloudflare IP instead of
the friend's.

**Groups still apply.** A device keeps its static address and its wgt
[groups](../README.md#groups-who-can-reach-what) whichever config it uses, so
`mom.iphone` reaches the same devices as `iphone`'s other configs. Only its
internet traffic goes through the puck: traffic to other devices and the server
stays on the VPS, under wgt's group rules. Groups are per server, so set them on
the VPS the puck's jump is on (`../bin/wgt-ctl dengar device group iphone home`).

**Check [client], either routing:** with the device's config on, and the
device off your home network (a phone on cellular), `curl https://ifconfig.me`
or the ifconfig.me page shows your home IP: the puck's network, for now, which
`tpk-ctl.sh test` prints as `this network's public IP`. With `wgt`, the
jump comes up only once a device connects through it, so this is its first
test from a real device (`tpk-ctl.sh test`'s end-to-end row tests the same path
from the VPS).

## 2.3 Optional: LAN mode

By default the puck is **isolated**: clients get the friend's internet and
nothing else. **LAN mode** also lets them reach devices on the friend's LAN by
IP: a NAS, a printer, the router's page. **Ask the friend first.**

It's one way (LAN devices can't reach your clients, and see every connection
as from the puck), by IP only (no AirPlay or network discovery, and names like
`nas.lan` don't resolve), and clients don't get an address on that LAN.

```sh
scripts/tpk-ctl.sh lan-mode lan          # on
scripts/tpk-ctl.sh lan-mode isolated     # off
scripts/tpk-ctl.sh mom.ig88 lan-mode lan # with several VPSes: for that one's clients
```

LAN mode is set per VPS: it switches the puck's interface for that VPS,
`wgpk<n>` (with `tpk`) or `wgrtun<n>` (with `wgt`); an interface name after
the mode picks another (`scripts/tpk-ctl.sh lan-mode lan wgrtunlan1`). Every client of that interface gets the same mode, and
the other VPSes' clients keep theirs. To choose per device with `wgt`, keep `wgrtun<n>`
isolated, set `JUMP_LAN="yes"` in `configs/<puck>.<vps>.conf`, and run
`scripts/tpk-ctl.sh exit` (`<puck>.<vps> exit` with several VPSes): it adds a
second jump, `wgrtunlan<n>`, in LAN
mode, and each device picks by config (`mom.phone` isolated, `mom-l.laptop`
LAN). The LAN jump's code is the jump's with `-l`, shortened to fit wgt's six
characters; set `JUMP_LAN_CODE` to name it yourself.

"The LAN" is the subnet the puck is on, so at home it's yours; the puck follows
when it moves. It must not overlap the client's own network (see
[Troubleshooting](#troubleshooting)).

**Check [Puck]:** `journalctl -u wg-quick@wgpk<n> -n 20` (or `@wgrtun<n>`; the ON THE PUCK column of `scripts/tpk-ctl.sh vps` names them) shows
`tunnel-puck-exit: ... LAN 192.168.1.50/24 reachable` (the puck's address on that
LAN).

## 2.4 Optional: more pucks, more VPSes

Skip this with one puck and one VPS. Both directions combine: any number of
pucks, each serving any of your VPSes.

### More than one tunnel-puck endpoint

One VPS can serve several pucks, say one at a friend's and one at your mom's.
Each puck gets its own settings files, and `vps add` leaves `TPK_LINK` and
`PUCK_PORT` empty in the new one: `link` picks a subnet and port nothing on the
VPS uses, and saves them in that file.

```sh
scripts/tpk-ctl.sh new home                    # then fill in configs/home.conf: SSH_LAN ...
scripts/tpk-ctl.sh home config
scripts/tpk-ctl.sh home init                   # and so on through the steps, naming the puck each time
scripts/tpk-ctl.sh home vps add dengar
```

Each puck has its own link on the VPS (`wgpk-home`, `wgpk-mom`), with its own
subnet, routing table and rules, so they don't touch each other.
`tpk-ctl.sh config` (and `link` and `exit`, which check first) also refuses a
clash with your other pucks' settings for the same VPS: the same subnet or
port, or (below) the same jump code or client. Two pucks' jumps on one VPS need
different codes, so set `JUMP_CODE` when their names share the first six
characters (`auntie-a` and `auntie-b` both shorten to `auntie`). How clients pick a puck depends on
routing:

- **`TPK_ROUTING=wgt`: each puck is another wgt jump, and a device picks its
  exit by config.** Each puck's jump is its name (`home`, `mom`), unless you
  set `JUMP_CODE`. One phone can have both configs and switch between them:

  ```sh
  scripts/tpk-ctl.sh home client phone      # home.phone: out through the friend's
  scripts/tpk-ctl.sh mom client phone       # mom.phone: out through your mom's
  ```

  This is plain wgt: it handles any number of jumps, and
  `../bin/wgt-ctl WGT_SERVER client list` shows every config and its exit.
- **`TPK_ROUTING=tpk`: each client address belongs to one puck.** List it in
  that puck's `CLIENT_IPS` only. A device that should switch between pucks needs a
  client config per puck on the VPS, each with its own address.

### One puck, several VPSes

One puck can also serve several VPSes: the clients of each come out at the
friend's. Add them one at a time; each is a VPS settings file of its own, its
own link, and with `wgt` its own reverse tunnel and jump, so adding or removing
one leaves the others alone.

```sh
scripts/tpk-ctl.sh mom vps add dengar      # the first
scripts/tpk-ctl.sh mom vps add ig88        # another: dengar is untouched
scripts/tpk-ctl.sh mom vps                 # what it serves
scripts/tpk-ctl.sh mom.ig88 client iphone  # a config on ig88 (mom.iphone there)
scripts/tpk-ctl.sh mom test                # the puck, then every VPS
scripts/tpk-ctl.sh mom vps remove ig88     # ig88's configs, jump and link, and configs/mom.ig88.conf
```

```
VPS          ROUTING  STATE    SERVER                   LINK            ON THE PUCK                JUMPS
dengar       wgt      serving  dengar                   10.99.0.0/30    wgpk0 wgrtun0              mom
ig88         wgt      serving  ig88                     10.99.0.4/30    wgpk1 wgrtun1              mom
```

- **Commands for one VPS** (`link`, `exit`, `client`, `lan-mode`) take it as
  `<puck>.<vps>`, or ask which when there are several (with no terminal: name
  it, or set `TPK_VPS`). `config`, `status`, `test`, `all`, `disable` and
  `enable` do every VPS, or one if named. The puck's own commands (`init`, `wifi`, `ssh`, `reboot`, `shutdown`,
  `undo`) don't depend on a VPS.
- **A device gets a config on each VPS it should use** (`dengar.mom.iphone`,
  `ig88.mom.iphone`): both come out at the puck, each through its own VPS. Its
  wgt groups and number are per server, as always.
- **Each link gets a subnet free on its VPS and different from the puck's other
  links**, which names its interfaces on the puck (`wgpk1`, `wgrtun1`). The jump
  code can be the same on each VPS: codes are per server.
- **The puck is reached through any of its links:** if one VPS is down,
  `tpk-ctl.sh` goes through another's. `vps remove` doesn't go through the link
  it removes.
- **The puck's bandwidth is shared** between the VPSes when several are busy.

---

# Phase 3: finish and prove it

## 3.1 Ethernet or Wi-Fi at the friend's **[Computer]**

The puck uses ethernet when it's plugged in, else any saved Wi-Fi in range, and
moves between them on its own. Choose now:

```sh
scripts/tpk-ctl.sh wifi                              # what's saved, and what the puck is using
scripts/tpk-ctl.sh wifi remove all                   # ethernet only: the usual choice
scripts/tpk-ctl.sh wifi add "<friend's network>"     # or their Wi-Fi too (asks for the password)
```

Ethernet only is the most reliable, and the friend sets up nothing. Their
Wi-Fi needs their password, and helps where a cable can't reach. `remove`
won't remove the network the puck is using right now: plug in ethernet first.

**Or keep a list:** several networks, most preferred first,
in `configs/<puck>.wifi.conf` next to the puck's settings (git ignores it, and
`tpk-ctl.sh` keeps it mode 600). One `name::password` per line, an empty
password for an open network:

```sh
cp configs/puck.wifi.conf.example configs/<puck>.wifi.conf   # or tpk-ctl.sh new made it; fill it in
scripts/tpk-ctl.sh wifi sync                                 # the puck's Wi-Fi becomes this list
```

Among networks in range, the puck joins the highest on the list, and moves on
to the next if one fails to connect; ethernet still wins when plugged in.
`sync` removes the networks tunnel-puck saved earlier that aren't on the list
(not the one in use right now) and leaves others, like Raspberry Pi Imager's,
alone. Edit the file and sync again to change it; `tpk-ctl.sh all` syncs it
too. With no `<puck>.wifi.conf`, the puck's Wi-Fi is left as it is.

**Check:** `scripts/tpk-ctl.sh test` shows `INFO  uplink: eth0. Saved Wi-Fi: ...`
as you expect.

## 3.2 Harden

- **Storage:** power cuts wear out SD cards. Use an SSD (USB boot) if you can.
  The SD-card alternative, raspi-config's overlay file system, also throws away
  every later change, `tpk-ctl.sh` commands and updates included, unless you
  turn it off first. If you use it, turn it on last, just before drop-off.
- **Updates:** `init` turned on automatic security updates.

## 3.3 The away-from-home test **[Hands]**, then **[Computer]** and **[client]**

Put the puck on a **phone hotspot**: like the friend's network, a NAT you don't
control. Use a travel router on the hotspot with the puck's ethernet in it, a
USB-to-ethernet adapter tethered to the phone, or the hotspot's Wi-Fi
(`scripts/tpk-ctl.sh wifi add "<hotspot>"` while still on ethernet, then
unplug; remove it again after the tests with
`scripts/tpk-ctl.sh wifi remove "<hotspot>"`, once ethernet is back in). No
reboot is needed: the puck follows the network change on its own.

Keep your computer and the client on another network. "The puck's IP" below
is the hotspot's, which `tpk-ctl.sh test` prints.

| # | Test | Pass |
|---|---|---|
| 1 | **[Computer]** `scripts/tpk-ctl.sh test` | `all tests passed`; its `puck: ... via` line says `via the link to <vps>`. With `wgt`, each VPS's end-to-end row shows the puck's IP |
| 2 | **[client]** (a puck-exit config) `curl https://ifconfig.me` | the puck's IP |
| 3 | **[client]** a DNS leak test (such as dnsleaktest.com) | `tpk`: only the puck's ISP. `wgt`: Cloudflare's resolvers, reached from the puck's IP |
| 4 | **[client]** `curl -6 -m 10 https://ifconfig.me` | fails: IPv6 is blocked |
| 5 | **[Computer]** stop the puck's end of one VPS's link. Find its interface in the ON THE PUCK column of `scripts/tpk-ctl.sh vps` (say `wgpk0`), then `scripts/tpk-ctl.sh ssh sudo systemctl stop wg-quick@wgpk0` (if `tpk-ctl.sh` came in through that link, the session then hangs: Ctrl-C). Then **[client]**, on a config of that VPS, `curl -m 10 https://ifconfig.me` | times out. It must **not** print the VPS's IP. Other VPSes' clients still work |
| 6 | the link still down: **[Computer]** `scripts/tpk-ctl.sh test` | its `puck: ... via` line says `via the link to <another vps>` or `via SSH_CLOUDFLARE`; with neither, it can't reach the puck. That VPS's rows FAIL |
| 7 | **[Computer]** `scripts/tpk-ctl.sh ssh sudo systemctl start wg-quick@wgpk0` (the interface from test 5; with no other way in: power-cycle the puck) | `tpk-ctl.sh test` passes again |
| 8 | `tpk` only: **[VPS]** `systemctl stop wg-quick@wgpk-mom` (`wgpk-<puck>`), **[client]** `curl -m 10 https://ifconfig.me`, **[VPS]** `systemctl start wg-quick@wgpk-mom` | times out (the kill switch). Never the VPS's IP |
| 9 | **[Puck]** `ip route` names the router after `default via`; **[client]** `ping -c2` it | isolated: no reply. LAN mode: replies |
| 10 | **[client]** a config that is **not** a puck exit: `curl https://ifconfig.me` | the same IP as before tunnel-puck |

With several VPSes, test 2 is worth running on a config of each.

## 3.4 Survive a power cut **[Hands]**

Unplug the puck and plug it back in.

**Check [Computer]:** a minute later, untouched, `scripts/tpk-ctl.sh test`
passes and test 2 passes again. Drop-off is the same power-up on another
network.

---

# Phase 4: drop-off **[Hands]**

The friend plugs in ethernet (unless you saved their Wi-Fi) and power. That's
all.

**Check [Computer], within a few minutes:** `scripts/tpk-ctl.sh test` passes
and shows the friend's IP, and so does a puck-exit client's
`curl https://ifconfig.me`. Tests 2–4 and 9–10 of [3.3](#33-the-away-from-home-test-hands-then-computer-and-client) are worth repeating. Run 5–8 there only with the emergency door working, or another VPS's link up: they cut a link, and without another way in only a power cycle brings it back.

If it doesn't come up: ask the friend to check the ethernet lights, then to
unplug the power and plug it back in. Still nothing usually means their
network blocks outbound UDP to `PUCK_PORT`. With the emergency door you'll see it
(`ssh SSH_CLOUDFLARE` works, `test` shows no `wgpk<n>` handshake): set
`PUCK_PORT="443"` in that VPS's settings (if free on the VPS) and run
`scripts/tpk-ctl.sh link` (`<puck>.<vps> link` with several). A puck with
another VPS's link still up is reached through that one instead. With no way
in at all, bring the puck home to change it.

---

# Day to day

- **Add or remove a `tpk` client:** edit `CLIENT_IPS` (and `CLIENT_IP6S`) in
  that VPS's settings file, then `scripts/tpk-ctl.sh link`. A removed client's
  rules are removed too.
- **Add a `wgt` device:** `scripts/tpk-ctl.sh client <device>` (`<puck>.<vps> client` with several VPSes; [2.2](#22-device-configs)).
- **Serve another VPS, or stop serving one:** `vps add <vps>`, `vps remove <vps>`
  ([One puck, several VPSes](#one-puck-several-vpses)).
- **After `wgt restore` or `wgt-ctl move` on the VPS:** wgt's backups include
  `wgpk-<puck>`, but restoring doesn't bring the link back up: run
  `scripts/tpk-ctl.sh <puck>.<vps> link`.
- **Your VPS's IP changes:** the puck looks up `VPS_HOST` (with `wgt`, the
  server's address in `../bin/wgt-ctl list`) only when the link starts. If it's
  an IP rather than a name, update it there and run
  `scripts/tpk-ctl.sh <puck>.<vps> link`. With a name, once DNS has the new IP: `scripts/tpk-ctl.sh ssh sudo systemctl restart wg-quick@wgpk<n>` (it
  goes through another VPS's link or Cloudflare, since this link is down), or
  have the friend power-cycle the puck.
- **Look around on the puck:** `scripts/tpk-ctl.sh ssh`, the first way that
  answers.
- **At a glance:** `scripts/tpk-ctl.sh status`, read-only and quick: how the
  puck was reached, its uplink, public IP and Wi-Fi, then per VPS whether it's
  serving, the link's and rtun's last handshakes and the jump's state, and
  under it each config on the jump with its last handshake (or `disabled`;
  with `tpk`, each of `CLIENT_IPS`, from the VPS's WireGuard). `test` says why
  something's off. `status --watch [seconds]` keeps it on screen, redrawn every
  5s (each round takes a few seconds of ssh), until Ctrl-C; on a wgt VPS,
  `../bin/wgt-ctl <vps> client list --watch` is the VPS's whole table, live.

  ```
  mom: via the link to dengar, through root@dengar.example.com
    uplink eth0, public IP 203.0.113.7, saved Wi-Fi: none
  VPS        STATE     LINK                 RTUN                  JUMP
  dengar     partly    wgpk0 19s ago        wgrtun0 8m ago        mom idle
    mom.iphone                   last handshake: 3h ago
    mom.laptop                   disabled
  ig88       serving   wgpk1 78s ago        wgrtun1 5m ago        mom up, no WARP (1 active)
    mom.iphone                   last handshake: 1m ago
  ```

## Turning clients off and on

Without deleting anything: keys, configs and addresses stay, and the puck, its
links and rtuns stay up (`test` still passes). A device that's off gets no
connection; one let back in works with the config it has, no new QR code.

| Turn off | Back on | What |
|---|---|---|
| `scripts/tpk-ctl.sh mom disable iphone` | `scripts/tpk-ctl.sh mom enable iphone` | one device's configs through the puck, on every VPS (or `mom.ig88 ...`: one); `wgt` VPSes only |
| `scripts/tpk-ctl.sh mom disable` | `scripts/tpk-ctl.sh mom enable` | every client of the puck, on every VPS (or one) |
| `../bin/wgt-ctl ig88 client disable mom.iphone` | `... client enable mom.iphone` | one config on a VPS, puck or not (wgt itself) |
| `../bin/wgt-ctl ig88 client disable --all` | `... client enable --all` | every config on a VPS, puck or not (wgt itself) |

With `wgt` they all run `wgt client disable` / `enable`. `enable` lets back in
only what its own `disable` turned off: `tpk-ctl.sh` keeps those configs' names in
the VPS's settings file (`TPK_DISABLED_CONFIGS`), and wgt keeps `--all`'s in
`clients.disabled-all`, so a config you turned off yourself stays off. A config
made while the puck is disabled works at once (`disable` again turns it off
too). `status` and `vps` show a VPS as `disabled` (`TPK_DISABLED="yes"` in its
settings file), or `partly` with only some devices off.

With `tpk`, `disable` is for the whole VPS only (`link` is rebuilt without
`CLIENT_IPS`'s routing, so the kill switch blocks them: no internet, never the
VPS's IP). For one device, take its address out of `CLIENT_IPS` and run
`link`.

# Undo **[Computer]**

```sh
scripts/tpk-ctl.sh vps remove dengar   # one VPS: its configs, jumps and link, and its settings file
scripts/tpk-ctl.sh undo                # every VPS's tunnel-puck parts, then the puck's WireGuard parts
scripts/tpk-ctl.sh undo all            # the same, plus cloudflared on the puck
```

With `wgt`, taking a VPS away also removes **the device configs that use the
puck's jumps there** (`<code>.<device>`, the ones `client` made), since
without the jump they lead nowhere. It names them and asks once before it
changes anything. They're gone for good: making one again issues new keys, so
the device needs its new config (or QR code). wgt-ctl keeps your old copy in
`~/wg-tunnels` as a `.bak`, but the VPS no longer accepts its key. If you
only need new rtun keys, or are replacing the puck, don't remove the VPS:
`TPK_RENEW=1 scripts/tpk-ctl.sh <puck>.<vps> exit` makes new rtun keys, and for
a reflashed puck you run `link`, then `exit`. Neither touches device configs.

None of these goes through a link it removes: they reach the puck through
another VPS's link, `SSH_LAN` or `SSH_CLOUDFLARE`. `undo` keeps the settings files, so `vps add` (or `all`)
builds it again. After `undo all`, delete the tunnel and the Access application
in the dashboard **[Browser]**.

**Check [VPS], on each VPS taken away:** `ip rule` shows none of this puck's
rules ([Fixed values](#fixed-values)), `wgt jump list` no longer shows the code(s), and clients
are back on their usual routes. `scripts/tpk-ctl.sh vps` no longer lists a
removed VPS.

# Troubleshooting

| Symptom | Look at |
|---|---|
| `test` says `via the link to ...` at home | `SSH_LAN` didn't answer: is the puck on, and does `ssh SSH_LAN true` work without a prompt? |
| `test` says `via SSH_CLOUDFLARE` | the links are down; you're in through the emergency door. The rows below say why |
| `can't reach <puck>: SSH_LAN ... didn't answer, nor did its links` | away from home, every link is down and there's no emergency door: the friend power-cycles the puck. At home: is `SSH_LAN` right? |
| `which VPS? tpk-ctl.sh <puck>.<vps> ...` | the puck serves several VPSes and there's no terminal to ask on: name one, or set `TPK_VPS` |
| `is the same VPS as ...: one settings file per VPS` | two of the puck's VPS files point at one server: remove one (`vps remove`, or delete the file if it was never built) |
| `also has TPK_LINK ..., and on the puck they must differ` | a VPS file copied from another: empty its `TPK_LINK` (and `PUCK_PORT`), and `link` picks new ones |
| `still holds a VPS's settings (the old layout)` | the puck's file is from before several VPSes: move its VPS settings (`TPK_ROUTING` on) into `configs/<puck>.<vps>.conf`, as in `configs/puck.vps.conf.example` |
| the puck can't be reached at all | with the door: is the tunnel Healthy in the dashboard, your email in the Access policy, is `cloudflared` installed on this computer? Without it: the friend power-cycles the puck |
| `tpk-ctl.sh` waits at "This changes the VPS ... Continue?" | run it in a terminal, or `TPK_YES=1` once the person has agreed |
| `10.99.0.x/30 is already used here` or `port ... in use` | something else on the VPS has them: empty `TPK_LINK` (or `PUCK_PORT`), and `link` picks a free one |
| FAIL `wgpk<n> has no recent handshake` | **[VPS]** is `PUCK_PORT/udp` open (`ss -ulpn`, ufw, the provider's firewall)? **[Puck]** does `VPS_HOST` resolve? Then `scripts/tpk-ctl.sh link` again |
| **[Puck]** `wg-quick@wgpk<n>` keeps restarting | `journalctl -u wg-quick@wgpk<n> -n 20`: `no default route yet` is no network; a failed lookup is `VPS_HOST` |
| `wgt jump add` fails | **[VPS]** `wg show wgpk-<puck>` (the link first: a recent handshake?), then `wg show px-JUMP_CODE` (it exists only while a device uses the jump); **[Puck]** `sudo wg show wgrtun<n>`. Then `TPK_RENEW=1 scripts/tpk-ctl.sh <puck>.<vps> exit` (new rtun keys) |
| `the puck's rtun exists but <vps> has no jump` | `TPK_RENEW=1 scripts/tpk-ctl.sh <puck>.<vps> exit` |
| `tpk`: handshake, but no internet on the client | **[VPS]** `ip rule` (this puck's rules, [Fixed values](#fixed-values)), `ip route show table <the puck's table>` ([Fixed values](#fixed-values)); **[Puck]** `sudo iptables -t nat -L -v`, `journalctl -u wg-quick@wgpk<n>` (its `tunnel-puck-exit:` line names the uplink) |
| `tpk`: sites load, names don't resolve | **[Puck]** `systemctl status unbound`; **[VPS]** `iptables -t nat -L PREROUTING -v` |
| large pages hang | MTU: a client config's `MTU` above 1280 needs lowering. The jump runs inside the link, so a packet much bigger than a client's 1280 gets split in two on the way, and behind a busy NAT that can cut the speed to a few Mbit/s |
| a client shows the VPS's IP | `tpk`: is it in `CLIENT_IPS`? `systemctl status tunnel-puck-killswitch-<puck>`. `wgt`: is the config named `<code>.<device>`? |
| traffic stops after the puck changes networks | **[Puck]** `journalctl -t tunnel-puck-exit -n 20` shows each move; if none, reboot the puck |
| LAN mode: some LAN addresses don't answer | the friend's LAN overlaps the client's own network (a café on `192.168.1.0/24` too), a subnet of `WG_IF`, or the link's addresses ([Fixed values](#fixed-values)). Try the client from another network |

---

# Reference

## Commands

All from **[Computer]**, as `scripts/tpk-ctl.sh [<puck>[.<vps>]] <command>`
(`<puck>` can be left out with one puck, `.<vps>` with one VPS; with several it
asks):

| Command | Does |
|---|---|
| `new [<name>] [--wifi]` | a new puck's settings file from the template (asks the name), and with `--wifi` or a yes its Wi-Fi list |
| `config` | prints the settings, the puck's and each VPS's, and checks them |
| `ssh-config` | the `~/.ssh/config` entry for `SSH_CLOUDFLARE`, printed |
| `ssh-config install` | every puck's entry into your ssh config (its own file, plus one `Include` line); not needed by `tpk-ctl.sh` itself |
| `init` | the puck: package updates, automatic security updates, the hardware watchdog, and with the door, cloudflared |
| `vps add <vps>` | serve a VPS: its settings file if missing (`wgt`, ready, for a server in `../bin/wgt-ctl list`; else `tpk`, to fill in), then `link`, `exit` and `test` for it |
| `vps [list]` | the VPSes the puck serves, from their settings files: routing, state (`serving`, `partly`, `disabled`, or `-` before `link`), server, link subnet, the puck's interfaces, jumps (`status` checks them live) |
| `vps remove <vps>` | with `wgt`, the device configs on the puck's jumps there and the jumps; its link on both ends; then its settings file (asks first) |
| `link` | one VPS. The VPS picks a free subnet (apart from the puck's other links) and port the first time. The puck: WireGuard, IP forwarding, a retry at boot, its key, and a hook that moves the exit rules when the network changes. The VPS: its key, its end of the link (`wgpk-<puck>`), `PUCK_PORT/udp` in ufw if active, and with `tpk` the routing and kill switch for `CLIENT_IPS`. Then the puck's end (`wgpk<n>`) |
| `exit` | one VPS. `tpk`: the DNS resolver (unbound) on the puck. `wgt`: the reverse tunnel(s) on the puck (`wgrtun<n>`), piped into `wgt jump add` on the VPS |
| `all` | `link` and `exit` for each VPS (or the one named), then `wifi sync` (only with a `<puck>.wifi.conf`), `test` |
| `client <device> [lan]` | `wgt`, one VPS: a config for the device (`lan`: through the LAN-mode jump) |
| `lan-mode isolated\|lan [<interface>]` | one VPS: switch LAN mode (default `wgpk<n>` or `wgrtun<n>`) |
| `disable [<device>]`, `enable [<device>]` | the puck's clients (or one device's) off and back on, on every VPS or the one named; nothing deleted ([Turning clients off and on](#turning-clients-off-and-on)) |
| `status` | at a glance, read-only: the puck, then per VPS (or the one named) its state, handshakes, jump and configs |
| `wifi`, `wifi add <name>`, `wifi remove <name>\|all` | the puck's saved Wi-Fi, one network at a time |
| `wifi sync` | the puck's saved Wi-Fi becomes `configs/<puck>.wifi.conf`, in its order |
| `test` | PASS/FAIL: the puck, then each VPS (or the one named) on both ends; with `wgt`, then a client's way out end to end (`wgt jump test`: the IP must be the puck's) |
| `ssh [<command>]` | a shell (or one command) on the puck, the first way that answers |
| `reboot` | restart the puck, and wait until it answers again |
| `shutdown` | power the puck off cleanly, safe to unplug (asks first: away from home, someone must replug it) |
| `undo [all]` | take it off every VPS, device configs included, as `vps remove` does, and the puck (`all`: cloudflared too); the settings files stay |

Environment variables:

| Variable | Does |
|---|---|
| `TPK_DRY_RUN=1` | shows what would run, and changes nothing |
| `TPK_YES=1` | answers yes to "This changes the VPS ... Continue?" |
| `TPK_RENEW=1` | with `exit`: makes new jump keys |
| `TPK_TAKEOVER=1` | with `exit`: lets this puck take over a jump code the VPS already uses for something else (normally refused, since `wgt jump add` would repoint it and `undo` would delete it) |
| `TPK_PUCK=<name>` | the puck to offer as the default when it asks; with no terminal to ask on, the puck it uses |
| `TPK_VPS=<vps>` | the same for which VPS, for a command about one VPS |

## How tpk-ctl.sh reaches the machines

**The puck:** every command, and `scripts/tpk-ctl.sh <puck> ssh` for a shell
(or `ssh <command>` for one command), takes the first way that answers, and
says which:

1. `SSH_LAN`, on your home network.
2. Through each VPS over its link in turn (`ssh -J VPS_SSH <user>@<the puck's link address>`).
3. `SSH_CLOUDFLARE`, the emergency door.

**The VPS:** with `tpk`, `VPS_SSH` over plain ssh. With `wgt`, through
`wgt-ctl`: its server list and host-key checks, and every wgt command as
`wgt-ctl WGT_SERVER ...`, so configs reach `~/wg-tunnels`. That needs
`tunnel-puck/` inside the wireguard-tools checkout, next to `bin/wgt-ctl`.

**The scripts:**

| Script | Runs |
|---|---|
| `scripts/tpk-ctl.sh` | on your computer: the management script, the one you run |
| `scripts/puck.sh` | on the puck, as root, sent by `tpk-ctl.sh` (the top says how to run it by hand) |
| `scripts/vps.sh` | on the VPS, as root, sent by `tpk-ctl.sh` (likewise) |
| `scripts/tunnel-puck-exit` | installed on the puck: the exit's firewall rules |

**The puck's host key**, when reached through the link, is kept under its name
(`tunnel-puck-<puck>`) in `~/.config/tunnel-puck/known_hosts`, not under its
link address: link addresses are reused, by a reflashed puck or a new puck on
an old one's subnet. `link` forgets it each time it builds the link, and the
next connection learns it again; the link itself is authenticated by
WireGuard's keys.

**Secrets** never go on a command line. The token and the Wi-Fi password
travel inside ssh into root-only files, and the jump file goes from the puck to
the VPS through a pipe.

## What the scripts change

**On the puck:**

| File or setting | Routing | Made by | What it is |
|---|---|---|---|
| `/etc/wireguard/wgpk<n>.*` | `tpk` or `wgt` | `link` | the link to one VPS: config and keys (with `tpk`, also the exit rules) |
| `/usr/local/sbin/tunnel-puck-exit` | `tpk` or `wgt` | `link` | the exit's firewall rules |
| `/etc/sysctl.d/99-tunnel-puck.conf` | `tpk` or `wgt` | `link` | IP forwarding on |
| `/etc/systemd/system/wg-quick@.service.d/tunnel-puck-retry.conf` | `tpk` or `wgt` | `link` | `wg-quick` keeps retrying at boot |
| `/etc/NetworkManager/dispatcher.d/90-tunnel-puck` | `tpk` or `wgt` | `link` | moves the exit rules when the network changes |
| `/etc/unbound/unbound.conf.d/tunnel-puck.conf`, `tunnel-puck-<n>.conf` | `tpk` | `exit` | the DNS resolver for puck-exit clients, and each link's address for it |
| `/etc/wireguard/wgrtun<n>.*`, `wgrtunlan<n>.*` | `wgt` | `exit` | the reverse tunnel(s) one VPS reaches in through (wgt's jump): config, keys and exit rules |
| `/etc/wireguard/jump-for-vps<n>*.conf` | `wgt` | `exit`, briefly | the jump file for the VPS, deleted once wgt has it |
| `RuntimeWatchdogSec=15` in `/etc/systemd/system.conf` | `tpk` or `wgt` | `init` | the hardware watchdog |
| `unattended-upgrades` | `tpk` or `wgt` | `init` | automatic security updates |
| Cloudflare's apt repository, `/etc/systemd/system/tunnel-puck-cloudflared.service` (when a Cloudflare Tunnel is configured, [1.3](#13-the-emergency-door-cloudflare-browser-skip-without-ssh_cloudflare)) | `tpk` or `wgt` | `init` | cloudflared and its service |
| `/etc/cloudflared/tunnel-puck.env` (when a Cloudflare Tunnel is configured, [1.3](#13-the-emergency-door-cloudflare-browser-skip-without-ssh_cloudflare)) | `tpk` or `wgt` | `init` | the tunnel token (mode 600) |
| `/etc/NetworkManager/system-connections/tunnel-puck-*.nmconnection` | `tpk` or `wgt` | `wifi add`, `wifi sync` | a saved Wi-Fi network, with its password (mode 600) |

**On the VPS:**

| File or setting | Routing | Made by | What it is |
|---|---|---|---|
| `/etc/wireguard/wgpk-<puck>.*` | `tpk` or `wgt` | `link` | the link to the puck: config and keys. With `tpk`, it also routes `CLIENT_IPS` to the puck and redirects their DNS |
| a ufw rule for `PUCK_PORT/udp` | `tpk` or `wgt` | `link`, if ufw is active | lets the puck dial in |
| `/usr/local/sbin/tunnel-puck-killswitch-<puck>`, `/etc/systemd/system/tunnel-puck-killswitch-<puck>.service` | `tpk` | `link` | the kill switch (with `wgt`, wgt's own fail-closed does this) |
| wgt's jumps and client configs | `wgt` | `exit`, `client` | made by wgt itself (`wgt jump add`, `wgt client add`) |

tunnel-puck never edits `WG_IF`'s configuration or wgt's files itself. With
`wgt`, every change on the VPS goes through `wgt-ctl`: the jumps `exit` adds,
the configs `client` makes, `disable`/`enable`, and their removal by
`vps remove` and `undo`.

## Fixed values

Names on the VPS come from the puck's name. Everything else comes from the
link's subnet, `TPK_LINK`: the n-th /30 in `10.99.0.0/24` (n from 0 to 63),
which `link` picks from what's free on the VPS and among the puck's other
links. They're chosen to avoid wgt's numbers (tables `100`, `200`–`250`, rules
`9300`–`10000`) and common setups.

| Value | From | `home`, `10.99.0.8/30` (n = 2) | What it is |
|---|---|---|---|
| link interface on the VPS | `wgpk-<puck>` | `wgpk-home` | the link, at the VPS's end |
| link interface on the puck | `wgpk<n>` | `wgpk2` | the link, at the puck's end |
| link addresses | `TPK_LINK`: VPS `+1`, puck `+2` | VPS `10.99.0.9`, puck `10.99.0.10` | the two ends of the link |
| reverse tunnels on the puck | `wgrtun<n>`, `wgrtunlan<n>`, UDP `51822 + 2n`, `51823 + 2n` | `wgrtun2` on `51826`, `wgrtunlan2` on `51827` | `wgt`: what the VPS reaches in through (its jumps) |
| jump addresses | `10.98.n.0/29`: puck `.1`, VPS `.2`; LAN-mode jump `.5`, `.6` | `10.98.2.1`, `10.98.2.2` | `wgt`: the ends of the reverse tunnels |
| routing table | `260 + n` | `262` | `tpk`: the table that goes to the puck |
| rule priorities | `900 + 2n`, `901 + 2n` | `904`, `905` | `tpk`: the rule sending a listed client to the table, and the kill switch that drops its traffic when the first can't apply |
| kill switch | `tunnel-puck-killswitch-<puck>` | `tunnel-puck-killswitch-home` | `tpk`: its script and service on the VPS |

## Why not carry WireGuard over Cloudflare?

WireGuard is UDP only, and a Cloudflare Tunnel carries HTTP, WebSocket and
TCP. Wrapping one in the other needs a helper on every client, which phones
can't run. So client traffic takes the link through your VPS, and Cloudflare
carries only your SSH, well inside its free plan and rules.

## Limits and care

- **Speed:** your downloads are the friend's uploads, so their upload speed
  caps yours, and every request takes the longer path client → VPS → puck.
- **The friend's IP changes:** fine. The puck dials out again.
- **Other things that give you away:** IP, DNS and IPv6 are covered. WebRTC,
  the browser's time zone and language, location services and account history
  aren't; strict services (banks, streaming) look at them.
- **The friend:** to their ISP, this traffic is their household's. Let only
  clients you trust use it, and tell the friend what the box does.
