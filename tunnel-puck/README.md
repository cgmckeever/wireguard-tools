# tunnel-puck: a small computer as a residential exit

Put a **puck**, a small single-board computer (SBC) like a Raspberry Pi, on a
friend's home network, and chosen clients of your WireGuard gateway reach the
internet **from the friend's home IP**. The friend only plugs in power and an
ethernet cable: no router settings, no open ports.

```
client ──WireGuard──▶ your gateway ──WireGuard link──▶ puck at the friend's ──▶ internet
                                      ▲ the puck dials out and keeps it open, so the friend's NAT doesn't matter
```

Your clients still connect to your gateway. The ones you choose get a config of
their own for it (with `tpk`, their address is listed instead), and the gateway
sends their traffic down a link the puck keeps open, and the puck sends it out
under the friend's IP. You build and test everything at home; at drop-off the
puck only needs power. One puck can serve several gateways, each over its own link
([One puck, several gateways](#one-puck-several-gateways)).

**Where each tunnel starts and ends, and who sees the traffic.** With `wgt`:

```
client ══[1]══▶ gateway wg0 ─ plain ─▶ gateway ══[2 inside 3]══▶ puck wgpk0 → wgrtun0 ─ plain ─▶ friend's ISP ──▶ sites
  [1] the client's config: client ↔ gateway
  [2] the jump (rtun): gateway px-<code> ↔ puck wgrtun<n>
  [3] the link: gateway wgpk-<puck> ↔ puck wgpk<n>, dialled by the puck
```

| Where | Sees the traffic in the clear? |
|---|---|
| the client's own network, its ISP | no: only [1], encrypted, to the gateway |
| **the gateway** | **yes**: it unwraps [1] and wraps the traffic again for the puck, as any VPN server would |
| between the gateway and the puck, the friend's router | no: [2] inside [3], encrypted twice |
| **the puck** | **yes**: it unwraps [3] then [2], and sends the traffic out |
| **the friend's ISP, and the sites** | **yes**, as for any traffic from that home (HTTPS still hides the content) |

With `tpk` there's no [2]: the gateway routes the listed clients straight into the
link, so it's [1], then [3] alone. Who sees what is the same.

To keep the traffic from the gateway too, the client would have to wrap it
itself, a puck tunnel inside its gateway tunnel, as
[wireguard-tunnels](https://github.com/cgmckeever/wireguard-tunnels)' warped
tunnels do with WARP. tunnel-puck doesn't do that. Phones would need
[sing-box](https://sing-box.sagernet.org) for it, and a Mac wireguard-tunnels,
sing-box or two `wg-quick` configs.

**Status:** tested only on a Raspberry Pi so far: a Pi 2 v1.1 (32-bit
Raspberry Pi OS Bookworm), with `TPK_ROUTING=wgt` against a test gateway. Phases 1
and 2 ran from a fresh image, and an iPhone on cellular came out with the
puck's home IP. Unplugged and moved behind a travel router, the puck came back
on its own and was reached through the link. The same puck then served a
second gateway alongside the first, each with its own link and jump, and the
iPhone went through both. Still untested: the rest of phase 3 (fail closed, a
DNS leak test), `tpk` routing and the Cloudflare door.
Run it first on a gateway nobody else depends on.

## Installation success

1. A chosen client's public IP (`curl https://ifconfig.me`) is the residential
   (or other) IP of the network where the tunnel-puck sits.
2. Its DNS lookups also leave from that IP.
3. When the link is down, that client gets no connection at all. It never
   falls back to the gateway's IP.
4. Nothing from the tunnel reaches devices on the friend's LAN, unless you
   add `--lan` ([Mode (2.3)](#23-optional-mode-egress-and-lan)).
5. The gateway's other clients, the ones not sent to the puck, still go out through
   the gateway as before.
6. You can reach the puck remotely.

Phase 3 tests each of these.

## Two choices along the way

- **Routing, for each gateway:** how the gateway sends clients into the puck,
  tunnel-puck's own rules (`tpk`) or a wgt jump (`wgt`). Chosen when you add
  the gateway ([2.1](#21-serve-a-gateway-computer)).
- **An emergency door (optional, recommended):** SSH to the puck through
  Cloudflare when its links are down ([1.2b](#12b-optional-the-emergency-door-cloudflare-browser)).

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
  Debian image your usual way instead of 1.1.

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
- **A WireGuard gateway:** an internet-reachable WireGuard server that you
  control, the one your clients connect to: a VPS or other cloud machine with a
  public IP (with `TPK_ROUTING=wgt`, one running wgt from this repo). Linux,
  with `wg-quick`, `iptables` and systemd (Debian and Ubuntu have all three),
  and root ssh (or passwordless `sudo`).
- **Your computer** (macOS or Linux) with `ssh`, and this folder.
- **For the emergency door:** a domain whose DNS is on Cloudflare, a Cloudflare
  Zero Trust account (free; signing up may ask for a card), and `cloudflared`
  on your computer (macOS: `brew install cloudflared`).

## How to read this

- **One command does the work:** `scripts/tpk-ctl.sh`, run on your computer in
  this folder. It ssh-es to the puck and the gateway and runs `puck.sh` and `gateway.sh`
  there. `scripts/tpk-ctl.sh` with no arguments lists its commands.
- **Each puck has a name, `<puck>`:** its settings file is
  `configs/<puck>.conf`, and each gateway it serves (`<gateway>`) has one more,
  `configs/<puck>.<gateway>.conf`. Commands take the name first,
  `scripts/tpk-ctl.sh mom test`, or `mom.dengar` for one gateway. Leave it out, as
  the steps below do, and `tpk-ctl.sh` uses the only puck (and gateway) there is,
  or with several asks which (`TPK_PUCK=<puck>` and `TPK_GATEWAY=<gateway>` set the
  defaults). It always says which it chose. More than one: see
  [More than one tunnel-puck endpoint](#more-than-one-tunnel-puck-endpoint) and
  [One puck, several gateways](#one-puck-several-gateways).
- **A setting's name stands for its value:** `ssh SSH_LAN` means
  `ssh pi@tunnel-puck.local`.
- **`bin/wgt-ctl`** is the repository's: from this folder, `../bin/wgt-ctl`.
- **The examples** use a puck called `mom` on the gateway `dengar`, a second gateway
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
| **[Gateway]** | a root shell on the gateway |
| **[client]** | a device connected to the gateway |
| **[Browser]** | the Cloudflare dashboard |
| **[Hands]** | something physical: flashing, plugging in, a phone hotspot |

### For an AI agent following this

- **Ask before touching the gateway.** `gateway add`, `gateway remove`, `link`, `all`,
  `client`, `disable`, `enable`, `undo`, and `exit` with `TPK_ROUTING=wgt`
  change it (`status` only reads it), and `test`
  with `wgt` brings its jump up for a moment. Other people's traffic may run
  through it. Get the person's go-ahead for that server first, read-only
  commands included, and suggest a test server. `tpk-ctl.sh` also asks on
  the terminal; once the person has agreed for this gateway, `TPK_YES=1` answers
  for you. Reaching the puck through the gateway changes nothing there, but tell the
  person it connects. Follow any rules the person has about which servers you
  may use.
- **Some steps need a person:** everything marked **[Browser]** or
  **[Hands]**, the browser login on the first `ssh SSH_CLOUDFLARE`, making
  client configs with `TPK_ROUTING=tpk`, filling in a `tpk` gateway's settings
  (`gateway add` stops for it), and typing a Wi-Fi password. Stop, say exactly
  what to do, and wait.
- **Name the puck** on every command (`scripts/tpk-ctl.sh home all`) once
  there's more than one, and the gateway too (`home.dengar`) once a puck serves
  more than one: with no terminal, `tpk-ctl.sh` can't ask.
- **Dry run first:** `TPK_DRY_RUN=1 scripts/tpk-ctl.sh all`.
- **Secrets stay put.** Never print a `*.key` file, a `PrivateKey` line, a jump
  file, the Cloudflare token or a Wi-Fi password. The scripts never do.
- **A FAIL means stop.** Report the command, its output and the matching
  Troubleshooting row. Don't improvise a fix on the gateway.

---

# Phase 1: puck config

## 1.1 Flash the puck **[Hands]**

On a Raspberry Pi: with Raspberry Pi Imager, write the OS from
[What you need](#what-you-need). (Another board: its Debian image, set up the
same way.) In Imager's settings: a hostname and user (say `tunnel-puck` and `pi`; they become
`SSH_LAN` in [1.2a](#12a-fill-in-the-settings)), and **SSH with your public
key only** (passwords off). Leave Wi-Fi out: step
[3.1](#31-ethernet-or-wi-fi-at-the-friends-computer) handles it. Boot the puck at
home, on ethernet.

**Check [Computer]:** `ssh pi@tunnel-puck.local true` (your user and hostname) works with no password prompt.

## 1.2 Set up the puck **[Computer]**

Three parts, in this order: the settings, the emergency door if you want one
(its token is a setting, and `init` installs it), then the check and `init`.

### 1.2a Fill in the settings

Each puck gets a settings file named for it, in `configs/` (git ignores them).
The name is up to 10 characters of `a-z`, `0-9` and `-`, starting with a
letter. `new` asks for it and makes the file from the template, and offers a
Wi-Fi list too ([3.1](#31-ethernet-or-wi-fi-at-the-friends-computer)). This
file holds the puck's own settings; each gateway it serves gets its own file in
[2.1](#21-serve-a-gateway-computer).

```sh
scripts/tpk-ctl.sh new           # or: scripts/tpk-ctl.sh new mom [--wifi]
```

Fill it in (each line has a comment):

| Setting | When | What to put |
|---|---|---|
| `SSH_LAN` | always | `user@hostname` for the puck on your home network: the ones you chose in [1.1](#11-flash-the-puck-hands) (`pi@tunnel-puck.local`) |
| `SSH_CLOUDFLARE` | emergency door | an SSH hostname on your Cloudflare domain (`ssh.pi.example.com`). Empty: no door, skip [1.2b](#12b-optional-the-emergency-door-cloudflare-browser) |
| `CF_TOKEN_FILE` | emergency door | where you'll save the tunnel token in [1.2b](#12b-optional-the-emergency-door-cloudflare-browser) (the default is fine) |

### 1.2b [Optional] The emergency door: Cloudflare **[Browser]**

**Recommended.** Normally you reach the puck through a gateway, over its link
(with several gateways, any of them). That fails exactly when the links are down
(a blocked port, a changed gateway IP, a bad change). A Cloudflare Tunnel gives
SSH to the puck that doesn't need the link. It costs a domain on Cloudflare
(about $10–15 a year; the rest is free), and this step. Without it, a dead link
means the friend power-cycles the puck, or you take it home. If you skip it, set
`PUCK_PORT="443"` in each gateway's settings file when you reach 2.1: networks
rarely block it.

In Cloudflare Zero Trust (`one.dash.cloudflare.com`). Cloudflare renames these
menus now and then; follow the nearest match.

1. **Networks → Tunnels → Create a tunnel**, type **Cloudflared**, name
   `tunnel-puck`. The install page shows a command with a long **token** in it.
   Save the token alone to `CF_TOKEN_FILE` **[Computer]**:

   ```sh
   mkdir -p ~/.config/tunnel-puck
   install -m 600 /dev/stdin ~/.config/tunnel-puck/cloudflared.token   # paste the token, then Ctrl-D
   ```

   Don't run the dashboard's install command: [1.2c](#12c-check-and-init) does that.
2. On the tunnel's **Public Hostname** tab (or **Published application
   routes**): add `SSH_CLOUDFLARE`, type `SSH`, URL `localhost:22`.
3. **Access → Applications → Add an application → Self-hosted**, for
   `SSH_CLOUDFLARE`, with one **Allow** policy: *Emails* = your email.

### 1.2c Check and init

```sh
scripts/tpk-ctl.sh config        # the settings: ends with "settings look complete"
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

**Check:** `config` ends with `settings look complete` and, with the emergency door, prints no note about `CF_TOKEN_FILE`. `init` ends with `init done`, and `reboot` ends with `<puck> is back`. With the emergency door: the dashboard shows the tunnel
**Healthy**, and `ssh SSH_CLOUDFLARE` opens a browser login once **[Browser]**,
then a shell.

---

# Phase 2: tunnel config

The puck stays at home for this phase.

## 2.1 Serve a gateway **[Computer]**

**Choose the routing:** how this gateway sends clients into the puck. Pick the
row that matches it:

| Your gateway | Set | What you get |
|---|---|---|
| any WireGuard server | `TPK_ROUTING="tpk"`, with `GATEWAY_SSH` and `GATEWAY_HOST` | tunnel-puck's own rules on the gateway. You make the clients' configs yourself on the gateway and list their addresses in `CLIENT_IPS` ([2.2](#22-device-configs)) |
| runs **wgt** (the wireguard-tools in this repository) | `TPK_ROUTING="wgt"`, with `WGT_SERVER` (its name in `bin/wgt-ctl list`) | the puck becomes a wgt **jump**: an extra way out on a wgt server, which a client config named `<code>.<device>` uses instead of the gateway's own. Everything on the gateway goes through `wgt-ctl`, and `tpk-ctl.sh client` makes each device its config, delivered to `~/wg-tunnels` |

With `wgt`, `gateway add` below fills these in for you.

```sh
scripts/tpk-ctl.sh gateway add dengar      # asks before it changes the gateway
```

`gateway add` makes the gateway's settings file, `configs/<puck>.<gateway>.conf`, from
`configs/puck.gateway.conf.example`:

- **A server in `../bin/wgt-ctl list`** (here `dengar`) gets `TPK_ROUTING="wgt"`
  and `WGT_SERVER` filled in, and `gateway add` carries on at once.
- **Any other name** gets a `tpk` file and stops: fill in the `tpk` settings
  below, then run `gateway add` again. (For a wgt server under another name, set
  `TPK_ROUTING="wgt"` and `WGT_SERVER` instead.)

Then:

- **It builds the link** at both ends, swapping the keys for you: `wgpk-<puck>`
  on the gateway, `wgpk<n>` on the puck.
- **Then the exit.** With `tpk`: the routing and kill switch for `CLIENT_IPS`
  on the gateway, and a DNS resolver on the puck. With `wgt`: a **reverse tunnel
  (rtun)** on the puck, `wgrtun<n>`, which the gateway reaches in through, added
  to wgt as jump `JUMP_CODE`.
- **Last, `test`** for that gateway. `link`, `exit` and `test` also run on their
  own; [Commands](#commands) lists what each does.
- **If a step fails,** fix what it says and run `gateway add` again: it picks up
  the settings file it made, and every step is safe to repeat.
- **For a second gateway,** run `gateway add` again with its name
  ([One puck, several gateways](#one-puck-several-gateways)).

The first `link` picks the link's subnet (`TPK_LINK`) and, if empty,
`PUCK_PORT`, from what's free on the gateway, and saves both in the gateway's settings
file. The subnet also names the puck's interfaces for this gateway: `n` is its
place in `10.99.0.0/24`, counting from 0 (`10.99.0.0/30` is `wgpk0` and
`wgrtun0`, `10.99.0.4/30` is `wgpk1` and `wgrtun1`; [Fixed values](#fixed-values)).
It's kept apart from the puck's links to other gateways, and from other pucks' on
the same gateway, so a puck's first link isn't always `wgpk0`: `scripts/tpk-ctl.sh gateway`
lists them.

The gateway's settings (each line of the file has a comment):

| Setting | When | What to put |
|---|---|---|
| `TPK_ROUTING` | always | `tpk` or `wgt` |
| `PUCK_PORT` | optional | the link's UDP port on the gateway. Empty: `link` picks a free one and saves it. `443`, if free, is rarely filtered |
| `TPK_LINK` | leave empty | the link's subnet: `link` picks one and saves it. Never change it once set |
| `GATEWAY_SSH` | `tpk` | how ssh reaches the gateway (`root@gateway.example.com`) |
| `GATEWAY_HOST` | `tpk` | the gateway's public name or IP, which the puck dials |
| `CLIENT_IPS` | `tpk` | the IPv4 `Address` of each client config that should exit through the puck. Can wait until [2.2](#22-device-configs) |
| `CLIENT_IP6S` | `tpk`, if any | those clients' IPv6 addresses: they get blocked, since the link is IPv4 only |
| `WG_IF` | `tpk` | the gateway's WireGuard interface for your clients (`wg show interfaces`; usually `wg0`) |
| `WG_SUBNET` | `tpk` | leave empty (it means every subnet `WG_IF` is on) |
| `WGT_SERVER` | `wgt` | the gateway's name in `../bin/wgt-ctl list`; its ssh target and address come from there. Empty: the file's `<gateway>` if wgt-ctl lists it, else `tpk-ctl.sh` asks (or adds a new server) and saves it |
| `JUMP_CODE`, `JUMP_LABEL` | `wgt`, optional | the puck's jump on the gateway (wgt's codes are 2–6 of `a-z`, `0-9`, `-`) and a label for it. Empty: the puck's name, the code shortened to 6 (`grandma`: `grandm`); `config` shows it. A code the gateway already uses for something else is refused |
| `JUMP_LAN` | `wgt`, optional | `yes`: a second jump with `--egress --lan` ([2.3](#23-optional-mode-egress-and-lan)), coded as the jump's with `-l` (`mom`: `mom-l`; `grandma`: `gran-l`) |
| `JUMP_LAN_CODE`, `JUMP_LAN_LABEL` | `wgt`, optional | name the LAN jump yourself (this also turns it on); the label defaults to the jump's with " (LAN)" |

Among `gateway add`'s output with `wgt`, `wgt jump add` also tests the new jump and prints
`jump '<code>' works: ip=... warp=on`. That IP is **Cloudflare's, not your
home's**: wgt gives every jump its own WARP account (it registers one on the
gateway) and tests through it, for `warp.` configs. tunnel-puck's configs are made
without WARP, so they go out with your home IP; `tpk-ctl.sh test`'s end-to-end
row is where you see that.

**Check:** `gateway add` ends with `all tests passed` (later, `scripts/tpk-ctl.sh
test` runs the same for every gateway), with handshakes on `wgpk<n>` and
`wgpk-<puck>` and, with `wgt`, on `wgrtun<n>`. With `wgt` its
last row tests a client's way out end to end, with no device needed: wgt brings
the jump up and traces through it without WARP, and the IP it sees must be the
puck's own (`PASS  clients exit from the puck's network: <IP>`).


## 2.2 Device configs

Each device that should exit through the puck needs a WireGuard config. How you
make it depends on your routing.

### With `TPK_ROUTING=tpk`: by hand **[Gateway]**, then **[Computer]**

tunnel-puck makes no client configs: you do, on your gateway, however you normally
make them. Then send them through the puck:

1. Make a client config for each device on the gateway (or pick existing ones).
2. Put each config's IPv4 `Address` in `CLIENT_IPS` (and any IPv6 address in
   `CLIENT_IP6S`) in that gateway's settings file, `configs/<puck>.<gateway>.conf`.
3. Run `scripts/tpk-ctl.sh link` again (`<puck>.<gateway> link` with several
   gateways): it adds the routing and the kill switch for them.

To add or remove a device later, edit `CLIENT_IPS` and run `link` again.

### With `TPK_ROUTING=wgt`: tpk-ctl.sh makes them **[Computer]**

The puck is a wgt jump on the gateway, coded `JUMP_CODE` (`<code>` below), which is
the puck's name unless you set it. Each device gets a config named
`<code>.<device>` on the gateway, delivered to your computer as
`<server>.<code>.<device>.conf` (`<server>` is `WGT_SERVER`). For a puck
called `mom` on the server `dengar`:

```sh
scripts/tpk-ctl.sh mom client iphone          # mom.iphone on dengar
scripts/tpk-ctl.sh mom client laptop lan      # with JUMP_LAN=yes: mom-l.laptop, egress and the LAN (2.3)
scripts/tpk-ctl.sh mom.ig88 client iphone     # when mom serves several gateways: on that one
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
stays on the gateway, under wgt's group rules. Groups are per server, so set them on
the gateway the puck's jump is on (`../bin/wgt-ctl dengar device group iphone home`).

**Check [client], either routing:** with the device's config on, and the
device off your home network (a phone on cellular), `curl https://ifconfig.me`
or the ifconfig.me page shows your home IP: the puck's network, for now, which
`tpk-ctl.sh test` prints as `this network's public IP`. With `wgt`, the
jump comes up only once a device connects through it, so this is its first
test from a real device (`tpk-ctl.sh test`'s end-to-end row tests the same path
from the gateway).

## 2.3 [Optional] Mode: egress and LAN

By default the puck gives clients **egress** only: the friend's internet and
nothing else. With **`--lan`** they can also reach devices on the friend's LAN
by IP: a NAS, a printer, the router's page. **Ask the friend first.**

It's one way (LAN devices can't reach your clients, and see every connection
as from the puck), by IP only (no AirPlay or network discovery, and names like
`nas.lan` don't resolve), and clients don't get an address on that LAN.

```sh
scripts/tpk-ctl.sh mode --egress --lan          # the internet and the LAN
scripts/tpk-ctl.sh mode --egress                # the internet only (the default)
scripts/tpk-ctl.sh mode                         # show it
scripts/tpk-ctl.sh mom.ig88 mode --egress --lan # with several gateways: for that one's clients
```

`--lan` alone (the LAN, with the internet from the gateway as usual) isn't
built yet: tpk-ctl.sh refuses it.

The mode is set per gateway: it switches the puck's interface for that gateway,
`wgpk<n>` (with `tpk`) or `wgrtun<n>` (with `wgt`); an interface name after the
flags picks another (`scripts/tpk-ctl.sh mode --egress --lan wgrtunlan1`).
Every client of that interface gets the same mode, and the other gateways'
clients keep theirs. To choose per device with `wgt`, keep `wgrtun<n>` at
`--egress`, set `JUMP_LAN="yes"` in `configs/<puck>.<gateway>.conf`, and run
`scripts/tpk-ctl.sh exit` (`<puck>.<gateway> exit` with several gateways): it
adds a second jump, `wgrtunlan<n>`, with `--egress --lan`, and each device
picks by config (`mom.phone` egress only, `mom-l.laptop` the LAN too). The LAN
jump's code is the jump's with `-l`, shortened to fit wgt's six characters; set
`JUMP_LAN_CODE` to name it yourself.

"The LAN" is the subnet the puck is on, so at home it's yours; the puck follows
when it moves. It must not overlap the client's own network (see
[Troubleshooting](#troubleshooting)).

**Check [Puck]:** `journalctl -u wg-quick@wgpk<n> -n 20` (or `@wgrtun<n>`; the ON THE PUCK column of `scripts/tpk-ctl.sh gateway` names them) shows
`tunnel-puck-exit: ... LAN 192.168.1.50/24 reachable` (the puck's address on that
LAN).

## 2.4 [Optional] More pucks, more gateways

Skip this with one puck and one gateway. Both directions combine: any number of
pucks, each serving any of your gateways.

### More than one tunnel-puck endpoint

One gateway can serve several pucks, say one at a friend's and one at your mom's.
Each puck gets its own settings files, and `gateway add` leaves `TPK_LINK` and
`PUCK_PORT` empty in the new one: `link` picks a subnet and port nothing on the
gateway uses, and saves them in that file.

```sh
scripts/tpk-ctl.sh new home                    # then fill in configs/home.conf: SSH_LAN ...
scripts/tpk-ctl.sh home config
scripts/tpk-ctl.sh home init                   # and so on through the steps, naming the puck each time
scripts/tpk-ctl.sh home gateway add dengar
```

Each puck has its own link on the gateway (`wgpk-home`, `wgpk-mom`), with its own
subnet, routing table and rules, so they don't touch each other.
`tpk-ctl.sh config` (and `link` and `exit`, which check first) also refuses a
clash with your other pucks' settings for the same gateway: the same subnet or
port, or (below) the same jump code or client. Two pucks' jumps on one gateway need
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
  client config per puck on the gateway, each with its own address.

### One puck, several gateways

One puck can also serve several gateways: the clients of each come out at the
friend's. Add them one at a time; each is a gateway settings file of its own, its
own link, and with `wgt` its own reverse tunnel and jump, so adding or removing
one leaves the others alone.

```sh
scripts/tpk-ctl.sh mom gateway add dengar   # the first
scripts/tpk-ctl.sh mom gateway add ig88     # another: dengar is untouched
scripts/tpk-ctl.sh mom gateway              # what it serves
scripts/tpk-ctl.sh mom.ig88 client iphone   # a config on ig88 (mom.iphone there)
scripts/tpk-ctl.sh mom test                 # the puck, then every gateway
scripts/tpk-ctl.sh mom gateway remove ig88  # ig88's configs, jump and link, and configs/mom.ig88.conf
```

```
GATEWAY      ROUTING  STATE    SERVER                   LINK            ON THE PUCK                JUMPS
dengar       wgt      serving  dengar                   10.99.0.0/30    wgpk0 wgrtun0              mom
ig88         wgt      serving  ig88                     10.99.0.4/30    wgpk1 wgrtun1              mom
```

- **Commands for one gateway** (`link`, `exit`, `client`, `mode`) take it as
  `<puck>.<gateway>`, or ask which when there are several (with no terminal: name
  it, or set `TPK_GATEWAY`). `config`, `status`, `test`, `all`, `disable` and
  `enable` do every gateway, or one if named. The puck's own commands (`init`, `wifi`, `ssh`, `reboot`, `shutdown`,
  `undo`) don't depend on a gateway.
- **A device gets a config on each gateway it should use** (`dengar.mom.iphone`,
  `ig88.mom.iphone`): both come out at the puck, each through its own gateway. Its
  wgt groups and number are per server, as always.
- **Each link gets a subnet free on its gateway and different from the puck's other
  links**, which names its interfaces on the puck (`wgpk1`, `wgrtun1`). The jump
  code can be the same on each gateway: codes are per server.
- **The puck is reached through any of its links:** if one gateway is down,
  `tpk-ctl.sh` goes through another's. `gateway remove` doesn't go through the link
  it removes.
- **The puck's bandwidth is shared** between the gateways when several are busy.

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
| 1 | **[Computer]** `scripts/tpk-ctl.sh test` | `all tests passed`; its `puck: ... via` line says `via the link to <gateway>`. With `wgt`, each gateway's end-to-end row shows the puck's IP |
| 2 | **[client]** (a puck-exit config) `curl https://ifconfig.me` | the puck's IP |
| 3 | **[client]** a DNS leak test (such as dnsleaktest.com) | `tpk`: only the puck's ISP. `wgt`: Cloudflare's resolvers, reached from the puck's IP |
| 4 | **[client]** `curl -6 -m 10 https://ifconfig.me` | fails: IPv6 is blocked |
| 5 | **[Computer]** stop the puck's end of one gateway's link. Find its interface in the ON THE PUCK column of `scripts/tpk-ctl.sh gateway` (say `wgpk0`), then `scripts/tpk-ctl.sh ssh sudo systemctl stop wg-quick@wgpk0` (if `tpk-ctl.sh` came in through that link, the session then hangs: Ctrl-C). Then **[client]**, on a config of that gateway, `curl -m 10 https://ifconfig.me` | times out. It must **not** print the gateway's IP. Other gateways' clients still work |
| 6 | the link still down: **[Computer]** `scripts/tpk-ctl.sh test` | its `puck: ... via` line says `via the link to <another gateway>` or `via SSH_CLOUDFLARE`; with neither, it can't reach the puck. That gateway's rows FAIL |
| 7 | **[Computer]** `scripts/tpk-ctl.sh ssh sudo systemctl start wg-quick@wgpk0` (the interface from test 5; with no other way in: power-cycle the puck) | `tpk-ctl.sh test` passes again |
| 8 | `tpk` only: **[Gateway]** `systemctl stop wg-quick@wgpk-mom` (`wgpk-<puck>`), **[client]** `curl -m 10 https://ifconfig.me`, **[Gateway]** `systemctl start wg-quick@wgpk-mom` | times out (the kill switch). Never the gateway's IP |
| 9 | **[Puck]** `ip route` names the router after `default via`; **[client]** `ping -c2` it | egress only: no reply. With `--lan`: replies |
| 10 | **[client]** a config that is **not** a puck exit: `curl https://ifconfig.me` | the same IP as before tunnel-puck |

With several gateways, test 2 is worth running on a config of each.

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
`curl https://ifconfig.me`. Tests 2–4 and 9–10 of [3.3](#33-the-away-from-home-test-hands-then-computer-and-client) are worth repeating. Run 5–8 there only with the emergency door working, or another gateway's link up: they cut a link, and without another way in only a power cycle brings it back.

If it doesn't come up: ask the friend to check the ethernet lights, then to
unplug the power and plug it back in. Still nothing usually means their
network blocks outbound UDP to `PUCK_PORT`. With the emergency door you'll see it
(`ssh SSH_CLOUDFLARE` works, `test` shows no `wgpk<n>` handshake): set
`PUCK_PORT="443"` in that gateway's settings (if free on the gateway) and run
`scripts/tpk-ctl.sh link` (`<puck>.<gateway> link` with several). A puck with
another gateway's link still up is reached through that one instead. With no way
in at all, bring the puck home to change it.

---

# Day to day

- **Add or remove a `tpk` client:** edit `CLIENT_IPS` (and `CLIENT_IP6S`) in
  that gateway's settings file, then `scripts/tpk-ctl.sh link`. A removed client's
  rules are removed too.
- **Add a `wgt` device:** `scripts/tpk-ctl.sh client <device>` (`<puck>.<gateway> client` with several gateways; [2.2](#22-device-configs)).
- **Serve another gateway, or stop serving one:** `gateway add <gateway>`, `gateway remove <gateway>`
  ([One puck, several gateways](#one-puck-several-gateways)).
- **After `wgt restore` or `wgt-ctl move` on the gateway:** wgt's backups include
  `wgpk-<puck>`, but restoring doesn't bring the link back up: run
  `scripts/tpk-ctl.sh <puck>.<gateway> link`.
- **Your gateway's IP changes:** the puck looks up `GATEWAY_HOST` (with `wgt`, the
  server's address in `../bin/wgt-ctl list`) only when the link starts. If it's
  an IP rather than a name, update it there and run
  `scripts/tpk-ctl.sh <puck>.<gateway> link`. With a name, once DNS has the new IP: `scripts/tpk-ctl.sh ssh sudo systemctl restart wg-quick@wgpk<n>` (it
  goes through another gateway's link or Cloudflare, since this link is down), or
  have the friend power-cycle the puck.
- **Look around on the puck:** `scripts/tpk-ctl.sh ssh`, the first way that
  answers.
- **At a glance:** `scripts/tpk-ctl.sh status`, read-only and quick: how the
  puck was reached, its uplink, public IP and Wi-Fi, then per gateway whether it's
  serving, the link's and rtun's last handshakes and the jump's state, and
  under it each config on the jump with its last handshake (or `disabled`;
  with `tpk`, each of `CLIENT_IPS`, from the gateway's WireGuard). `test` says why
  something's off. `status --watch [seconds]` keeps it on screen, redrawn every
  5s (each round takes a few seconds of ssh), until Ctrl-C. `status --alert`
  is the same with the terminal's bell when a config comes on (its last
  handshake under 3 minutes, after it wasn't), and a line saying which; it's
  silent otherwise. On a wgt gateway, `../bin/wgt-ctl <gateway> client list
  --watch` (or `--alert`) is the gateway's whole table, live.

  ```
  mom: via the link to dengar, through root@dengar.example.com
    uplink eth0, public IP 203.0.113.7, saved Wi-Fi: none
  GATEWAY    STATE     LINK                 RTUN                  JUMP
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
| `scripts/tpk-ctl.sh mom disable iphone` | `scripts/tpk-ctl.sh mom enable iphone` | one device's configs through the puck, on every gateway (or `mom.ig88 ...`: one); `wgt` gateways only |
| `scripts/tpk-ctl.sh mom disable` | `scripts/tpk-ctl.sh mom enable` | every client of the puck, on every gateway (or one) |
| `../bin/wgt-ctl ig88 client disable mom.iphone` | `... client enable mom.iphone` | one config on a gateway, puck or not (wgt itself) |
| `../bin/wgt-ctl ig88 client disable --all` | `... client enable --all` | every config on a gateway, puck or not (wgt itself) |

With `wgt` they all run `wgt client disable` / `enable`. `enable` lets back in
only what its own `disable` turned off: `tpk-ctl.sh` keeps those configs' names in
the gateway's settings file (`TPK_DISABLED_CONFIGS`), and wgt keeps `--all`'s in
`clients.disabled-all`, so a config you turned off yourself stays off. A config
made while the puck is disabled works at once (`disable` again turns it off
too). `status` and `gateway` show a gateway as `disabled` (`TPK_DISABLED="yes"` in its
settings file), or `partly` with only some devices off.

With `tpk`, `disable` is for the whole gateway only (`link` is rebuilt without
`CLIENT_IPS`'s routing, so the kill switch blocks them: no internet, never the
gateway's IP). For one device, take its address out of `CLIENT_IPS` and run
`link`.

# Undo **[Computer]**

```sh
scripts/tpk-ctl.sh gateway remove dengar  # one gateway: its configs, jumps and link, and its settings file
scripts/tpk-ctl.sh undo                   # every gateway's tunnel-puck parts, then the puck's WireGuard parts
scripts/tpk-ctl.sh undo all               # the same, plus cloudflared on the puck
```

With `wgt`, taking a gateway away also removes **the device configs that use the
puck's jumps there** (`<code>.<device>`, the ones `client` made), since
without the jump they lead nowhere. It names them and asks once before it
changes anything. They're gone for good: making one again issues new keys, so
the device needs its new config (or QR code). wgt-ctl keeps your old copy in
`~/wg-tunnels` as a `.bak`, but the gateway no longer accepts its key. If you
only need new rtun keys, or are replacing the puck, don't remove the gateway:
`TPK_RENEW=1 scripts/tpk-ctl.sh <puck>.<gateway> exit` makes new rtun keys, and for
a reflashed puck you run `link`, then `exit`. Neither touches device configs.

None of these goes through a link it removes: they reach the puck through
another gateway's link, `SSH_LAN` or `SSH_CLOUDFLARE`. `undo` keeps the settings files, so `gateway add` (or `all`)
builds it again. After `undo all`, delete the tunnel and the Access application
in the dashboard **[Browser]**.

**Check [Gateway], on each gateway taken away:** `ip rule` shows none of this puck's
rules ([Fixed values](#fixed-values)), `wgt jump list` no longer shows the code(s), and clients
are back on their usual routes. `scripts/tpk-ctl.sh gateway` no longer lists a
removed gateway.

# Troubleshooting

| Symptom | Look at |
|---|---|
| `test` says `via the link to ...` at home | `SSH_LAN` didn't answer: is the puck on, and does `ssh SSH_LAN true` work without a prompt? |
| `test` says `via SSH_CLOUDFLARE` | the links are down; you're in through the emergency door. The rows below say why |
| `can't reach <puck>: SSH_LAN ... didn't answer, nor did its links` | away from home, every link is down and there's no emergency door: the friend power-cycles the puck. At home: is `SSH_LAN` right? |
| `which gateway? tpk-ctl.sh <puck>.<gateway> ...` | the puck serves several gateways and there's no terminal to ask on: name one, or set `TPK_GATEWAY` |
| `is the same gateway as ...: one settings file per gateway` | two of the puck's gateway files point at one server: remove one (`gateway remove`, or delete the file if it was never built) |
| `also has TPK_LINK ..., and on the puck they must differ` | a gateway file copied from another: empty its `TPK_LINK` (and `PUCK_PORT`), and `link` picks new ones |
| the puck can't be reached at all | with the door: is the tunnel Healthy in the dashboard, your email in the Access policy, is `cloudflared` installed on this computer? Without it: the friend power-cycles the puck |
| `tpk-ctl.sh` waits at "This changes the gateway ... Continue?" | run it in a terminal, or `TPK_YES=1` once the person has agreed |
| `10.99.0.x/30 is already used here` or `port ... in use` | something else on the gateway has them: empty `TPK_LINK` (or `PUCK_PORT`), and `link` picks a free one |
| FAIL `wgpk<n> has no recent handshake` | **[Gateway]** is `PUCK_PORT/udp` open (`ss -ulpn`, ufw, the provider's firewall)? **[Puck]** does `GATEWAY_HOST` resolve? Then `scripts/tpk-ctl.sh link` again |
| **[Puck]** `wg-quick@wgpk<n>` keeps restarting | `journalctl -u wg-quick@wgpk<n> -n 20`: `no default route yet` is no network; a failed lookup is `GATEWAY_HOST` |
| `wgt jump add` fails | **[Gateway]** `wg show wgpk-<puck>` (the link first: a recent handshake?), then `wg show px-JUMP_CODE` (it exists only while a device uses the jump); **[Puck]** `sudo wg show wgrtun<n>`. Then `TPK_RENEW=1 scripts/tpk-ctl.sh <puck>.<gateway> exit` (new rtun keys) |
| `the puck's rtun exists but <gateway> has no jump` | `TPK_RENEW=1 scripts/tpk-ctl.sh <puck>.<gateway> exit` |
| `tpk`: handshake, but no internet on the client | **[Gateway]** `ip rule` (this puck's rules, [Fixed values](#fixed-values)), `ip route show table <the puck's table>` ([Fixed values](#fixed-values)); **[Puck]** `sudo iptables -t nat -L -v`, `journalctl -u wg-quick@wgpk<n>` (its `tunnel-puck-exit:` line names the uplink) |
| `tpk`: sites load, names don't resolve | **[Puck]** `systemctl status unbound`; **[Gateway]** `iptables -t nat -L PREROUTING -v` |
| large pages hang | MTU: a client config's `MTU` above 1280 needs lowering. The jump runs inside the link, so a packet much bigger than a client's 1280 gets split in two on the way, and behind a busy NAT that can cut the speed to a few Mbit/s |
| a client shows the gateway's IP | `tpk`: is it in `CLIENT_IPS`? `systemctl status tunnel-puck-killswitch-<puck>`. `wgt`: is the config named `<code>.<device>`? |
| traffic stops after the puck changes networks | **[Puck]** `journalctl -t tunnel-puck-exit -n 20` shows each move; if none, reboot the puck |
| with `--lan`: some LAN addresses don't answer | the friend's LAN overlaps the client's own network (a café on `192.168.1.0/24` too), a subnet of `WG_IF`, or the link's addresses ([Fixed values](#fixed-values)). Try the client from another network |

---

# Reference

## Commands

All from **[Computer]**, as `scripts/tpk-ctl.sh [<puck>[.<gateway>]] <command>`
(`<puck>` can be left out with one puck, `.<gateway>` with one gateway; with several it
asks):

| Command | Does |
|---|---|
| `new [<name>] [--wifi]` | a new puck's settings file from the template (asks the name), and with `--wifi` or a yes its Wi-Fi list |
| `config` | prints the settings, the puck's and each gateway's, and checks them |
| `ssh-config` | the `~/.ssh/config` entry for `SSH_CLOUDFLARE`, printed |
| `ssh-config install` | every puck's entry into your ssh config (its own file, plus one `Include` line); not needed by `tpk-ctl.sh` itself |
| `init` | the puck: package updates, automatic security updates, the hardware watchdog, and with the door, cloudflared |
| `gateway add <gateway>` | serve a gateway: its settings file if missing (`wgt`, ready, for a server in `../bin/wgt-ctl list`; else `tpk`, to fill in), then `link`, `exit` and `test` for it |
| `gateway [list]` | the gateways the puck serves, from their settings files: routing, state (`serving`, `partly`, `disabled`, or `-` before `link`), server, link subnet, the puck's interfaces, jumps (`status` checks them live) |
| `gateway remove <gateway>` | with `wgt`, the device configs on the puck's jumps there and the jumps; its link on both ends; then its settings file (asks first) |
| `link` | one gateway. The gateway picks a free subnet (apart from the puck's other links) and port the first time. The puck: WireGuard, IP forwarding, a retry at boot, its key, and a hook that moves the exit rules when the network changes. The gateway: its key, its end of the link (`wgpk-<puck>`), `PUCK_PORT/udp` in ufw if active, and with `tpk` the routing and kill switch for `CLIENT_IPS`. Then the puck's end (`wgpk<n>`) |
| `exit` | one gateway. `tpk`: the DNS resolver (unbound) on the puck. `wgt`: the reverse tunnel(s) on the puck (`wgrtun<n>`), piped into `wgt jump add` on the gateway |
| `all` | `link` and `exit` for each gateway (or the one named), then `wifi sync` (only with a `<puck>.wifi.conf`), `test` |
| `client <device> [lan]` | `wgt`, one gateway: a config for the device (`lan`: through the LAN-mode jump) |
| `mode [--egress [--lan]] [<interface>]` | one gateway: the internet only, or the friend's LAN too (default `wgpk<n>` or `wgrtun<n>`); no flags: show it |
| `disable [<device>]`, `enable [<device>]` | the puck's clients (or one device's) off and back on, on every gateway or the one named; nothing deleted ([Turning clients off and on](#turning-clients-off-and-on)) |
| `status` | at a glance, read-only: the puck, then per gateway (or the one named) its state, handshakes, jump and configs |
| `wifi`, `wifi add <name>`, `wifi remove <name>\|all` | the puck's saved Wi-Fi, one network at a time |
| `wifi sync` | the puck's saved Wi-Fi becomes `configs/<puck>.wifi.conf`, in its order |
| `test` | PASS/FAIL: the puck, then each gateway (or the one named) on both ends; with `wgt`, then a client's way out end to end (`wgt jump test`: the IP must be the puck's) |
| `ssh [<command>]` | a shell (or one command) on the puck, the first way that answers |
| `reboot` | restart the puck, and wait until it answers again |
| `shutdown` | power the puck off cleanly, safe to unplug (asks first: away from home, someone must replug it) |
| `undo [all]` | take it off every gateway, device configs included, as `gateway remove` does, and the puck (`all`: cloudflared too); the settings files stay |

Environment variables:

| Variable | Does |
|---|---|
| `TPK_DRY_RUN=1` | shows what would run, and changes nothing |
| `TPK_YES=1` | answers yes to "This changes the gateway ... Continue?" |
| `TPK_RENEW=1` | with `exit`: makes new jump keys |
| `TPK_TAKEOVER=1` | with `exit`: lets this puck take over a jump code the gateway already uses for something else (normally refused, since `wgt jump add` would repoint it and `undo` would delete it) |
| `TPK_PUCK=<name>` | the puck to offer as the default when it asks; with no terminal to ask on, the puck it uses |
| `TPK_GATEWAY=<gateway>` | the same for which gateway, for a command about one gateway |

## How tpk-ctl.sh reaches the machines

**The puck:** every command, and `scripts/tpk-ctl.sh <puck> ssh` for a shell
(or `ssh <command>` for one command), takes the first way that answers, and
says which:

1. `SSH_LAN`, on your home network.
2. Through each gateway over its link in turn (`ssh -J GATEWAY_SSH <user>@<the puck's link address>`).
3. `SSH_CLOUDFLARE`, the emergency door.

**The gateway:** with `tpk`, `GATEWAY_SSH` over plain ssh. With `wgt`, through
`wgt-ctl`: its server list and host-key checks, and every wgt command as
`wgt-ctl WGT_SERVER ...`, so configs reach `~/wg-tunnels`. That needs
`tunnel-puck/` inside the wireguard-tools checkout, next to `bin/wgt-ctl`.

**The scripts:**

| Script | Runs |
|---|---|
| `scripts/tpk-ctl.sh` | on your computer: the management script, the one you run |
| `scripts/puck.sh` | on the puck, as root, sent by `tpk-ctl.sh` (the top says how to run it by hand) |
| `scripts/gateway.sh` | on the gateway, as root, sent by `tpk-ctl.sh` (likewise) |
| `scripts/tunnel-puck-exit` | installed on the puck: the exit's firewall rules |

**The puck's host key**, when reached through the link, is kept under its name
(`tunnel-puck-<puck>`) in `~/.config/tunnel-puck/known_hosts`, not under its
link address: link addresses are reused, by a reflashed puck or a new puck on
an old one's subnet. `link` forgets it each time it builds the link, and the
next connection learns it again; the link itself is authenticated by
WireGuard's keys.

**Secrets** never go on a command line. The token and the Wi-Fi password
travel inside ssh into root-only files, and the jump file goes from the puck to
the gateway through a pipe.

## What the scripts change

**On the puck:**

| File or setting | Routing | Made by | What it is |
|---|---|---|---|
| `/etc/wireguard/wgpk<n>.*` | `tpk` or `wgt` | `link` | the link to one gateway: config and keys (with `tpk`, also the exit rules) |
| `/usr/local/sbin/tunnel-puck-exit` | `tpk` or `wgt` | `link` | the exit's firewall rules |
| `/etc/sysctl.d/99-tunnel-puck.conf` | `tpk` or `wgt` | `link` | IP forwarding on |
| `/etc/systemd/system/wg-quick@.service.d/tunnel-puck-retry.conf` | `tpk` or `wgt` | `link` | `wg-quick` keeps retrying at boot |
| `/etc/NetworkManager/dispatcher.d/90-tunnel-puck` | `tpk` or `wgt` | `link` | moves the exit rules when the network changes |
| `/etc/unbound/unbound.conf.d/tunnel-puck.conf`, `tunnel-puck-<n>.conf` | `tpk` | `exit` | the DNS resolver for puck-exit clients, and each link's address for it |
| `/etc/wireguard/wgrtun<n>.*`, `wgrtunlan<n>.*` | `wgt` | `exit` | the reverse tunnel(s) one gateway reaches in through (wgt's jump): config, keys and exit rules |
| `/etc/wireguard/jump-for-gateway<n>*.conf` | `wgt` | `exit`, briefly | the jump file for the gateway, deleted once wgt has it |
| `RuntimeWatchdogSec=15` in `/etc/systemd/system.conf` | `tpk` or `wgt` | `init` | the hardware watchdog |
| `unattended-upgrades` | `tpk` or `wgt` | `init` | automatic security updates |
| Cloudflare's apt repository, `/etc/systemd/system/tunnel-puck-cloudflared.service` (when a Cloudflare Tunnel is configured, [1.2b](#12b-optional-the-emergency-door-cloudflare-browser)) | `tpk` or `wgt` | `init` | cloudflared and its service |
| `/etc/cloudflared/tunnel-puck.env` (when a Cloudflare Tunnel is configured, [1.2b](#12b-optional-the-emergency-door-cloudflare-browser)) | `tpk` or `wgt` | `init` | the tunnel token (mode 600) |
| `/etc/NetworkManager/system-connections/tunnel-puck-*.nmconnection` | `tpk` or `wgt` | `wifi add`, `wifi sync` | a saved Wi-Fi network, with its password (mode 600) |

**On the gateway:**

| File or setting | Routing | Made by | What it is |
|---|---|---|---|
| `/etc/wireguard/wgpk-<puck>.*` | `tpk` or `wgt` | `link` | the link to the puck: config and keys. With `tpk`, it also routes `CLIENT_IPS` to the puck and redirects their DNS |
| a ufw rule for `PUCK_PORT/udp` | `tpk` or `wgt` | `link`, if ufw is active | lets the puck dial in |
| `/usr/local/sbin/tunnel-puck-killswitch-<puck>`, `/etc/systemd/system/tunnel-puck-killswitch-<puck>.service` | `tpk` | `link` | the kill switch (with `wgt`, wgt's own fail-closed does this) |
| wgt's jumps and client configs | `wgt` | `exit`, `client` | made by wgt itself (`wgt jump add`, `wgt client add`) |

tunnel-puck never edits `WG_IF`'s configuration or wgt's files itself. With
`wgt`, every change on the gateway goes through `wgt-ctl`: the jumps `exit` adds,
the configs `client` makes, `disable`/`enable`, and their removal by
`gateway remove` and `undo`.

## Fixed values

Names on the gateway come from the puck's name. Everything else comes from the
link's subnet, `TPK_LINK`: the n-th /30 in `10.99.0.0/24` (n from 0 to 63),
which `link` picks from what's free on the gateway and among the puck's other
links. They're chosen to avoid wgt's numbers (tables `100`, `200`–`250`, rules
`9300`–`10000`) and common setups.

| Value | From | `home`, `10.99.0.8/30` (n = 2) | What it is |
|---|---|---|---|
| link interface on the gateway | `wgpk-<puck>` | `wgpk-home` | the link, at the gateway's end |
| link interface on the puck | `wgpk<n>` | `wgpk2` | the link, at the puck's end |
| link addresses | `TPK_LINK`: gateway `+1`, puck `+2` | gateway `10.99.0.9`, puck `10.99.0.10` | the two ends of the link |
| reverse tunnels on the puck | `wgrtun<n>`, `wgrtunlan<n>`, UDP `51822 + 2n`, `51823 + 2n` | `wgrtun2` on `51826`, `wgrtunlan2` on `51827` | `wgt`: what the gateway reaches in through (its jumps) |
| jump addresses | `10.98.n.0/29`: puck `.1`, gateway `.2`; LAN-mode jump `.5`, `.6` | `10.98.2.1`, `10.98.2.2` | `wgt`: the ends of the reverse tunnels |
| routing table | `260 + n` | `262` | `tpk`: the table that goes to the puck |
| rule priorities | `900 + 2n`, `901 + 2n` | `904`, `905` | `tpk`: the rule sending a listed client to the table, and the kill switch that drops its traffic when the first can't apply |
| kill switch | `tunnel-puck-killswitch-<puck>` | `tunnel-puck-killswitch-home` | `tpk`: its script and service on the gateway |

## Why not carry WireGuard over Cloudflare?

WireGuard is UDP only, and a Cloudflare Tunnel carries HTTP, WebSocket and
TCP. Wrapping one in the other needs a helper on every client, which phones
can't run. So client traffic takes the link through your gateway, and Cloudflare
carries only your SSH, well inside its free plan and rules.

## Limits and care

- **Speed:** your downloads are the friend's uploads, so their upload speed
  caps yours, and every request takes the longer path client → gateway → puck.
- **The friend's IP changes:** fine. The puck dials out again.
- **Other things that give you away:** IP, DNS and IPv6 are covered. WebRTC,
  the browser's time zone and language, location services and account history
  aren't; strict services (banks, streaming) look at them.
- **The friend:** to their ISP, this traffic is their household's. Let only
  clients you trust use it, and tell the friend what the box does.
