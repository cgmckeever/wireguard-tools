# tunnel-pi: a Raspberry Pi as a residential exit

## Goal

Put a Raspberry Pi on a friend's home network so that chosen WireGuard clients
reach the internet **from the friend's home IP**. The friend only plugs in power
and an ethernet cable. They change no router settings and open no ports.

**Everything is built and tested at home.** At drop-off the Pi is finished:
it gets an address by DHCP, dials out, and every connection comes up on its
own. After drop-off, SSH over Cloudflare is for maintenance only.

The scripts in `scripts/` do the work. Once the Pi has an OS and the Cloudflare
dashboard steps are done, the rest is a few commands from your Mac:

```sh
cp tunnel-pi.conf.example tunnel-pi.conf    # fill it in
scripts/tp config                           # check the settings
scripts/tp base                             # the Pi: updates, watchdog, cloudflared
scripts/tp all                              # the link, the exit, the checks
scripts/tp client phone                     # wgt jump only: a config through the Pi
scripts/tp check                            # any time: PASS/FAIL on both ends
```

**Done when** all of these hold (phase 3 checks each one):

1. `ssh ADMIN_HOST` reaches the Pi from anywhere, even when WireGuard is down.
2. A chosen client's public IP (`curl https://ifconfig.me`) is the friend's home IP.
3. That client's DNS lookups also leave from the friend's IP.
4. When the Pi's link is down, that client loses its connection rather than
   showing the VPS's IP (it fails closed).
5. No traffic from the tunnel can reach devices on the friend's LAN, unless
   you turn on LAN mode (2D), in which case clients reach them by IP.
6. Clients that aren't chosen are unaffected.

## How it works

```
client ──WG (wg0)──▶ VPS ──WG (wgpi)──▶ Pi at friend's ──uplink──▶ internet
                              ▲ the Pi dials out (keepalive), so the friend's NAT/CGNAT doesn't matter
you ──ssh via cloudflared──▶ Cloudflare ──▶ cloudflared on the Pi ──▶ sshd
```

- **cloudflared** gives you SSH to the Pi. It carries admin traffic only.
- **wgpi** is a WireGuard link that the Pi dials out to the VPS and keeps open.
  Through it, the VPS can reach the Pi at `10.99.0.2`.
- **The exit:** the VPS sends the chosen clients' traffic over the link, and
  the Pi NATs it out of its uplink (`eth0` at the friend's) with the friend's
  IP. `EXIT` in the settings picks one of two ways:
  - **`jump` (2C):** the Pi also runs a small WireGuard "jump server" on its
    link address. The VPS adds it as an ordinary wgt **jump**, and wgt does
    the rest: per-device routing, fail-closed, IPv6, and configs named
    `<code>.<device>`.
  - **`hand` (2B):** the VPS gets hand-written policy routing, a DNS redirect
    and a kill switch for the client addresses you list.

**Which one?** On a wgt server, choose **`jump`**: there's less routing to
write by hand, and the result shows in `wgt client list`. It is new ground,
though: a jump reached over another tunnel hasn't been tried with wgt yet, so
prove it on bossk. Choose `hand` for a VPS without wgt.

**Why not carry WireGuard over cloudflared?** WireGuard only uses UDP, and a
Cloudflare Tunnel only carries HTTP, WebSocket and TCP. Wrapping WireGuard to
fit means running a helper on every client, which phones can't do.

## What's here

| File | Runs on | Does |
|---|---|---|
| `tunnel-pi.conf.example` | | the settings; copy it to `tunnel-pi.conf` (git ignores that name) |
| `scripts/tp` | your Mac | drives everything over ssh: streams `pi.sh` and `vps.sh` with the settings, swaps public keys, moves the jump file Pi → VPS through a pipe |
| `scripts/pi.sh` | the Pi (root) | `base`, `link`, `jump`, `hand`, `lan-mode`, `check`, `undo` |
| `scripts/vps.sh` | the VPS (root) | `link`, `check`, `undo` |
| `scripts/tunnel-pi-exit` | the Pi | the exit rules: forward to the uplink, NAT, isolated or LAN mode. `tp` installs it as `/usr/local/sbin/tunnel-pi-exit` |

Every step is safe to run again: files are rewritten only when they change,
and existing keys are kept. `TP_DRY_RUN=1 scripts/tp <step>` shows what would
run without changing anything. `pi.sh` and `vps.sh` can also run on their
machine without `tp`: the top of each file says how.

## What you need

- A Raspberry Pi 4 or 5 on wired ethernet, an SD card or SSD, and a power supply.
- A VPS your clients already connect to (a wgt server for `EXIT=jump`).
  **Build and test on bossk.** This adds routing next to wgt's on a server
  carrying live traffic.
- A domain whose DNS is on Cloudflare, and a Cloudflare Zero Trust account.
- On your Mac: `ssh` access to the Pi (with passwordless `sudo`, which
  Raspberry Pi OS gives the user it creates) and to the VPS (root, or `sudo`).

### What Cloudflare costs

- **Cloudflare Tunnel (cloudflared):** free, with no bandwidth charges.
- **Zero Trust / Access:** the free plan covers up to 50 users, which is plenty
  for SSH-only admin. Signing up may ask for a payment method even on the free
  plan.
- **A domain on Cloudflare:** this is the one real cost. Registration runs about
  $10–15 a year at any registrar, and Cloudflare's DNS for it is free. A domain
  you already own works too: just add a hostname like `ssh.pi.yourdomain.com`.

Your client traffic doesn't go through Cloudflare. It travels over WireGuard
from the VPS to the Pi, and only the SSH admin traffic uses the tunnel. That
keeps you well inside the free plan and clear of Cloudflare's rules against
pushing bulk traffic through tunnels.

## Settings

`tunnel-pi.conf` holds every value, with a comment on each. `scripts/tp
config` prints them, and stops if one is empty or still the example. The ones
to look up:

| Setting | How to find it |
|---|---|
| `PI_SSH` | how ssh reaches the Pi: its LAN name (`pi@tunnel-pi.local`) while you build, `ADMIN_HOST` afterwards |
| `VPS_SSH`, `VPS_HOST` | how ssh reaches the VPS, and the name or IP the Pi dials |
| `PI_PORT` | a UDP port on the VPS nothing listens on (`ss -ulpn`). wgt uses `51820`. `443` is a good choice if it's free: networks that filter UDP usually still allow it |
| `JUMP_CODE` | `EXIT=jump`: 2–6 characters of `a-z`, `0-9`, `-`, not one `wgt jump list` already shows |
| `CLIENT_IPS` | `EXIT=hand`: **[VPS]** `wgt client list`, the address of each config to route through the Pi (the `Address` in that config, not its static `10.9.8.x` address) |
| `WG_SUBNET` | `EXIT=hand`: leave it empty, and `vps.sh` reads it from `/etc/wireguard/wg-tools.state` |
| `CF_TOKEN_FILE` | a file outside the repo, mode `600`, holding only the tunnel token from step 1.2 |

These are fixed, chosen to stay clear of wgt (`wgt/const.py`):

| Fixed value | What it is |
|---|---|
| `10.99.0.0/30` | the link: VPS `10.99.0.1`, Pi `10.99.0.2`. `vps.sh` stops if the VPS already uses it |
| `10.98.0.0/30`, UDP `51822` | `jump`: the jump server (`10.98.0.1` on the Pi, `10.98.0.2` for the VPS). The LAN-mode jump uses `10.98.0.4/30` and `51823` |
| table `260`, rule priorities `900`/`901` | `hand`: wgt uses tables `100` and `200`–`250`, and priorities `9300`–`10000` |
| interfaces `wgpi`, `wgjump`, `wgjumplan` | wgt's are `wg0`, `warp`, `px-*`, `wx-*` |

## Where each step runs

| Marker | Where |
|---|---|
| **[Mac]** | your own machine, in this folder. Every `scripts/tp` command runs here |
| **[Pi]** | a shell on the Pi (`ssh PI_SSH`), for looking around: `tp` does the work |
| **[VPS]** | a root shell on the VPS |
| **[client]** | a device connected through the VPS |
| **[Browser]** | the Cloudflare dashboard. A person does this: it needs a login |
| **[Hands]** | something physical: flashing, plugging in, the phone hotspot |

Steps run in order. Each one ends with a **Check**. Don't go on until it passes.

## For an AI agent following this

- **Ask before touching the VPS.** `tp link`, `tp exit`, `tp client` and
  `tp undo` change the VPS, a server other people's traffic may run through.
  Get the person's go-ahead for each server first, read-only commands
  included. `tp` asks too, on the terminal. Once the person has agreed for
  this VPS, `TP_YES=1` answers for you. In this repo, that means bossk only
  (see `CLAUDE.md`): dengar, ig88 and boba are live, and this setup isn't
  proven on them.
- **Some steps need a person:** everything marked **[Browser]** or
  **[Hands]**, and the Cloudflare browser login on the first `ssh ADMIN_HOST`.
  Stop at each one, say exactly what to do, and wait.
- **Settings first.** Fill in `tunnel-pi.conf` with the person, then run
  `scripts/tp config` until it says the settings look complete.
- **Dry run first.** `TP_DRY_RUN=1 scripts/tp all` shows every remote step
  without running it.
- **Keys stay put.** Never print or log a `*.key` file, a `PrivateKey` line,
  a jump file or the Cloudflare token. The scripts never do: `tp` moves the
  jump file Pi → VPS through a pipe, and the token travels inside ssh's input
  into a root-only file.
- **A FAIL means stop.** Report the command, its output and the matching
  Troubleshooting row. Don't improvise a fix on the VPS.

---

# Phase 1: base and admin

## 1.1 Flash the Pi **[Hands]**

Use Raspberry Pi Imager to write **Raspberry Pi OS Lite (64-bit)**. In its
settings, set the hostname (`tunnel-pi`), the user (`PI_USER`), and **SSH with
your public key only**, with passwords off. An SSD (USB boot) lasts much longer
than an SD card (see 3.3). Boot the Pi on your home network.

**Check [Mac]:** `ssh PI_SSH true` succeeds, with `PI_SSH` the LAN name.

## 1.2 Cloudflare Tunnel and Access **[Browser]**

In Cloudflare Zero Trust (`one.dash.cloudflare.com`). Cloudflare renames these
menus from time to time, so follow the nearest match:

1. Go to **Networks → Tunnels → Create a tunnel**, choose **Cloudflared**, and
   name it `tunnel-pi`. The install page shows a command with a long
   **token**. Copy the token only (not the command) into `CF_TOKEN_FILE`:
   `install -m 600 /dev/stdin ~/.config/tunnel-pi/cloudflared.token`, then
   paste it and press Ctrl-D. Create the folder first if it doesn't exist.
   Skip the dashboard's install command: `tp base` does that part.
2. On the tunnel's **Public Hostname** tab (newer dashboards: **Published
   application routes**), add `ADMIN_HOST` → type `SSH`, URL `localhost:22`.
3. Go to **Access → Applications → Add an application → Self-hosted** for
   `ADMIN_HOST`, with one **Allow** policy: *Emails* = your email.

## 1.3 The Pi's base **[Mac]**

```sh
scripts/tp base
ssh PI_SSH sudo reboot          # once, so the hardware watchdog starts
scripts/tp ssh-config           # paste what it prints into ~/.ssh/config
```

`tp base` updates the Pi and turns on automatic security updates. It also turns
on the hardware watchdog and installs cloudflared. Then it starts cloudflared
as `tunnel-pi-cloudflared.service`, with the token in a root-only file.

**Check:** the dashboard shows the tunnel as **Healthy**, and `ssh ADMIN_HOST`
opens a browser login once **[Browser]**, then gives a shell on the Pi. From
here on you can set `PI_SSH="ADMIN_HOST's value"` in `tunnel-pi.conf`, or keep
the LAN name while the Pi is at home.

---

# Phase 2: WireGuard **[Mac]**

The Pi is still on your home network. The away-from-home test comes in
phase 3.

## 2A–2C. The link and the exit

```sh
TP_DRY_RUN=1 scripts/tp all     # see what it will do
scripts/tp all                  # asks before it changes the VPS
```

`tp all` runs three steps, each of which can also run on its own:

- **`tp link`** (2A):
  - On the Pi: installs WireGuard and turns on IP forwarding. Makes `wg-quick`
    retry at boot, so a slow DHCP or DNS can't leave the link down. Installs
    `tunnel-pi-exit` and makes the Pi's key.
  - On the VPS: makes its key and writes and starts `wgpi`, then opens
    `PI_PORT/udp` (in ufw, if it's active). With `EXIT=hand`, it also writes
    the routing for `CLIENT_IPS` and the kill switch (2B).
  - Then it writes and starts the Pi's end. Public keys are swapped for you.
- **`tp exit`** (2B or 2C):
  - `EXIT=jump`: makes the jump server on the Pi, and pipes its jump file
    straight into `wgt jump add JUMP_CODE` on the VPS, then deletes it. With
    `JUMP_LAN_CODE` set, it does the same for a second jump in LAN mode (2D).
  - `EXIT=hand`: installs the DNS resolver (unbound) on the Pi, which answers
    the clients' DNS.
- **`tp check`**: PASS/FAIL lines for both ends.

**Check:** `tp check` ends with `all checks passed`. The Pi's `wgpi` handshake
is recent, and each end answers a ping over the link. With `EXIT=jump`, the
`wgt jump add` output ended with `jump 'JUMP_CODE' works: ip=<your home IP>`.

**When the VPS runs wgt:** wgt doesn't manage `wgpi`, and leaves rules it
didn't make alone. Its backups archive all of `/etc/wireguard`, so `wgpi.conf`
and its key go in, but `wgt restore` or `move` won't start the tunnel-pi parts.
Run `scripts/tp link` again afterwards. With `EXIT=hand`, don't also give a
listed client a jump: priority 900 wins, so the jump would never be used.

## 2C (jump only). Give devices a config through the Pi **[Mac]**

```sh
scripts/tp client phone          # makes JUMP_CODE.phone
scripts/tp client laptop lan     # the LAN jump, if JUMP_LAN_CODE is set: JUMP_LAN_CODE.laptop
```

This runs `wgt client add <device> --exit <code> --warp off` on the VPS. With
`--warp off`, wgt makes only the plain `<code>.<device>` config. The `warp.`
version would put WARP inside the jump, and sites would see a Cloudflare IP
near the friend, not the friend's IP. Get the config onto the device with
`wgt client show <code>.<device> --qr` or `wgt client share <code>.<device>`
on the VPS (or `wgt-ctl`).

**What wgt provides:** blocking when the jump is down, IPv6 blocked through
the jump, and a listing in `wgt client list`. DNS goes to the config's `DNS`
(by default `1.1.1.1`) through the jump, so lookups leave from the friend's IP
too.

With `EXIT=hand` there's no `tp client`: every config listed in `CLIENT_IPS`
already goes through the Pi. To change the list, edit `CLIENT_IPS` and run
`scripts/tp link` again.

## 2D. Optional: LAN mode (reach the friend's LAN)

By default the Pi is **isolated**: clients get the friend's internet
connection, and nothing on the friend's LAN can be reached. In **LAN mode**,
clients can also reach devices on that LAN by IP: a NAS, a printer, a Plex
server, the router's admin page. **Ask the friend first:** this lets your
devices into their home network.

**What LAN mode is, and isn't:**

- **One way.** Your clients can open connections to LAN devices. LAN devices
  can't open connections to your clients, and see every connection as coming
  from the Pi's address.
- **By IP, not discovery.** It works at the IP layer, so broadcast and mDNS
  discovery don't cross it: AirPlay, Chromecast, "Network" browsing in Finder.
  Connect by address instead (`smb://192.168.1.20`, `http://192.168.1.1`).
- **Local names don't resolve.** Names like `nas.lan` belong to the friend's
  router. Use IPs.
- **Not full membership.** A client doesn't get an address on their LAN. That
  would need a layer-2 bridge over the tunnel, which is beyond this guide.

**Turn it on or off [Mac]:**

```sh
scripts/tp lan-mode lan          # EXIT=hand: wgpi. EXIT=jump: wgjump
scripts/tp lan-mode isolated     # back again
```

It changes one word in the interface's config and restarts that interface.
cloudflared doesn't depend on it, so this is safe after drop-off. "The LAN" is
the subnet of the Pi's uplink when the interface starts, so at home it's your
LAN.

**Who gets it.** The Pi sees every client as one address (the VPS's NAT), so
the mode applies to all clients of that interface. For some clients only, with
`EXIT=jump`: leave `wgjump` isolated and set `JUMP_LAN_CODE` (and
`JUMP_LAN_LABEL`). `tp exit` then adds a second jump, `wgjumplan`, in LAN mode.
Each device picks its mode by config: `home.phone` (isolated) or
`homelan.laptop` (LAN).

**Check [Pi]:** `journalctl -u wg-quick@wgjump -n 20` (or `@wgpi`) shows
`tunnel-pi-exit: ... LAN <subnet> reachable`.

**Watch for overlapping subnets.** LAN mode works only when the friend's LAN
(often `192.168.0.0/24` or `192.168.1.0/24`) overlaps none of these:

- **The client's own network.** A laptop on a café network that also uses
  `192.168.1.0/24` sends those addresses to the café, not to the tunnel. Use
  the client from another network (a phone hotspot works).
- **The VPS's client subnet or wgt's `10.9.8.0/24`.**
- **The links,** `10.99.0.0/30` and `10.98.0.0/29`.

---

# Phase 3: prove and drop off

## 3.1 The full test, on a phone hotspot **[Hands]**, then **[Mac]**/**[client]**

Move the Pi onto a **phone hotspot**. It copies the friend's network: a NAT
you don't control and no open ports. Any of these works:

- a travel router joined to the hotspot, with the Pi's ethernet plugged into
  it (closest to the friend's setup);
- a USB-to-ethernet adapter tethered to the phone;
- the hotspot's Wi-Fi, set up on the Pi with `sudo nmtui`. **Remove it after**
  (also with `nmtui`), so at the friend's the Pi uses ethernet only.

Reboot the Pi after the move, so the exit rules pick up the new uplink. Run the
tests from your Mac and client on a **different** network from the hotspot.
Here, "the Pi network's public IP" is the hotspot's: `tp check` prints it.

| # | Test | Pass |
|---|---|---|
| 1 | **[Mac]** `scripts/tp check` (with `PI_SSH` set to `ADMIN_HOST`) | `all checks passed` |
| 2 | **[client]** (a Pi-exit config) `curl https://ifconfig.me` | the Pi network's public IP |
| 3 | **[client]** a DNS leak test site (for example dnsleaktest.com) | `hand`: only the Pi network's ISP. `jump`: Cloudflare resolvers, reached from the Pi's IP |
| 4 | **[client]** `curl -6 -m 10 https://ifconfig.me` | fails (IPv6 blocked) |
| 5 | **[Pi]** `sudo systemctl stop wg-quick@wgpi`, then **[client]** `curl -m 10 https://ifconfig.me` | times out. It must **not** print the VPS's IP |
| 6 | with wgpi still stopped: **[Mac]** `ssh ADMIN_HOST` | still a shell. Then **[Pi]** `sudo systemctl start wg-quick@wgpi` |
| 6b | `hand` only: **[VPS]** `systemctl stop wg-quick@wgpi`, then **[client]** `curl -m 10 https://ifconfig.me`, then **[VPS]** `systemctl start wg-quick@wgpi` | times out (the kill switch). It must **not** print the VPS's IP |
| 7 | **[client]** `ping -c2` an address on the Pi's LAN (its router: `ip route` on the Pi shows it after `default via`) | isolated: no reply. LAN mode: replies |
| 8 | **[client]** a config that is **not** a Pi exit: `curl https://ifconfig.me` | unchanged: the VPS's or WARP's IP, as before |

## 3.2 Survive a power cut **[Hands]**

Unplug the Pi's power and plug it back in, as it will be at the friend's.

**Check [Mac]:** about a minute later, with no one touching the Pi,
`scripts/tp check` passes and test 2 passes again. A Pi that passes this is
ready: drop-off is the same power-up on a different network.

## 3.3 Harden

- **The SD card:** power cuts wear it out. The best fix is an SSD (USB boot).
  With an SD card, `sudo raspi-config` → Performance → Overlay file system
  protects it, but then nothing is saved across reboots, including the
  updates from `unattended-upgrades`. Each change, update included, means
  turning the overlay off remotely, rebooting, changing, and turning it back
  on.
- **Updates:** `tp base` turned on `unattended-upgrades`, which keeps
  installing security fixes while the root file system is writable.
- After any change here, repeat 3.2.

## 3.4 Drop-off **[Hands]**

The friend plugs in ethernet and power. That's all. Everything comes up on its
own.

**Check [Mac], within a few minutes:** `scripts/tp check` passes and shows the
friend's IP, and a Pi-exit client's `curl https://ifconfig.me` shows the same.
Running tests 2–8 of 3.1 again is a good idea.

If it doesn't come up, ask the friend to check that the ethernet link lights
are on, then to unplug the power and plug it back in. If it still doesn't come
up, their network is blocking something the hotspot didn't: usually outbound
UDP to `PI_PORT`. If `ssh ADMIN_HOST` works but `tp check` shows no `wgpi`
handshake, the port is blocked. Set `PI_PORT="443"` (if it's free on the VPS)
and run `scripts/tp link` again: it updates both ends.

---

## Undo **[Mac]**

```sh
scripts/tp undo          # the VPS's tunnel-pi parts, then the Pi's WireGuard parts
scripts/tp undo all      # ...and cloudflared on the Pi
```

With `EXIT=jump`, remove the devices' configs first: wgt won't remove a jump
that configs still use (`wgt client remove <code>.<device>` on the VPS for each
config `wgt client list` shows). `tp undo` then removes the jump(s), `wgpi`, and
with `EXIT=hand` the kill switch, and closes `PI_PORT` in ufw. After `undo all`,
delete the tunnel and the Access application in the dashboard **[Browser]**.

**Check [VPS]:** `ip rule` shows nothing at `900`/`901`, `wgt jump list` no
longer shows the code(s), and the clients are back on their normal routes.

## Troubleshooting

| Symptom | Look at |
|---|---|
| `ssh ADMIN_HOST` fails | is the tunnel Healthy in the dashboard? Does the Access policy include your email? If you can reach the Pi another way: `systemctl status tunnel-pi-cloudflared`. Otherwise the friend unplugs it and plugs it back in |
| `tp` stops at "This changes the VPS ... Continue?" with no terminal | it needs an answer: run it in a terminal, or set `TP_YES=1` once the person has agreed |
| `vps.sh: 10.99.0.0/30 is already used` or `port ... in use` | pick another `PI_PORT`, or free the link range on the VPS |
| FAIL `wgpi has no recent handshake` | **[VPS]** is `PI_PORT/udp` open (`ss -ulpn`, ufw, the provider's firewall)? **[Pi]** does `VPS_HOST` resolve? Run `scripts/tp link` again: it swaps the keys again |
| **[Pi]** `wg-quick@wgpi` keeps restarting | `journalctl -u wg-quick@wgpi -n 20`: `no default route yet` means no network; a failed name lookup means `VPS_HOST` doesn't resolve |
| `wgt jump add` fails | **[VPS]** `ping 10.99.0.2` (the link first), then `wg show px-JUMP_CODE`. **[Pi]** `sudo wg show wgjump`. Fix it, then `TP_RENEW=1 scripts/tp exit` makes new jump keys and tries again |
| `the Pi's jump server exists but the VPS has no jump` | the jump was removed on the VPS: `TP_RENEW=1 scripts/tp exit` |
| `hand`: handshake, but the client has no internet | **[VPS]** `ip rule` (900 first?), `ip route show table 260`. **[Pi]** `sudo iptables -t nat -L -v`, `journalctl -u wg-quick@wgpi` (the `tunnel-pi-exit:` line names the uplink) |
| `hand`: sites load but names don't resolve | **[Pi]** `systemctl status unbound`. **[VPS]** `iptables -t nat -L PREROUTING -v` |
| Some sites hang (usually large pages) | MTU. wgt's client configs use 1280, which fits through both tunnels. A config with a larger `MTU` needs lowering |
| A client shows the VPS's IP | `hand`: is its address in `CLIENT_IPS`? `systemctl status tunnel-pi-killswitch`. `jump`: is the config named `<code>.<device>`? |
| After moving the Pi between networks, traffic stops | the exit rules follow the uplink chosen when the interface started: reboot the Pi |

## What the scripts change

**On the Pi:** `/etc/wireguard/wgpi.*`, `wgjump*`, `wgjumplan*`, and briefly
`jump-for-vps*.conf`. Also `/usr/local/sbin/tunnel-pi-exit`,
`/etc/sysctl.d/99-tunnel-pi.conf`,
`/etc/systemd/system/wg-quick@.service.d/tunnel-pi-retry.conf`, and
`/etc/unbound/unbound.conf.d/tunnel-pi.conf` (`hand`). `tp base` adds
`/etc/systemd/system/tunnel-pi-cloudflared.service`,
`/etc/cloudflared/tunnel-pi.env` (the token, mode 600), Cloudflare's apt
repository, `RuntimeWatchdogSec=15` in `/etc/systemd/system.conf`, and
automatic updates.

**On the VPS:** `/etc/wireguard/wgpi.*`, a ufw rule for `PI_PORT/udp`, and with
`EXIT=hand`, `/usr/local/sbin/tunnel-pi-killswitch` and
`/etc/systemd/system/tunnel-pi-killswitch.service`. With `EXIT=jump`, wgt's
own `jump add` / `client add`. Nothing touches `wg0` or wgt's files.

## Limits and care

- **Speed:** your downloads are the friend's uploads, so their upload speed
  sets your ceiling. Every request also takes the extra trip
  client → VPS → Pi.
- **Their IP changes:** this is fine. The Pi dials out again and the VPS
  learns the new address.
- **Your VPS's IP changes:** `wg-quick` looks up `VPS_HOST` only when it
  starts: `ssh ADMIN_HOST sudo systemctl restart wg-quick@wgpi`.
- **Other things that reveal you:** the IP, DNS and IPv6 are covered. WebRTC,
  the browser's time zone and locale, phone location services and your account
  history aren't. Strict services (banks, streaming) look at these.
- **The friend:** to their ISP, all of this traffic is their household's. Let
  only clients you trust use this exit, and tell the friend what the box does.
