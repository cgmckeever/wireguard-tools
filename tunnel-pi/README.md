# tunnel-pi: a Raspberry Pi as a residential exit

## Goal

Put a Raspberry Pi on a friend's home network so that chosen WireGuard clients
reach the internet **from the friend's home IP**. The friend only plugs in power
and an ethernet cable. They change no router settings and open no ports.

**You build and test everything at home.** When you drop the Pi off it's
already finished: it gets an address by DHCP, dials out, and every connection
comes up on its own.

**Done when** all of these hold (phase 3 checks each one):

1. A chosen client's public IP (`curl https://ifconfig.me`) is the friend's home IP.
2. That client's DNS lookups also leave from the friend's IP.
3. When the Pi's link is down, that client loses its connection. It never
   falls back to the VPS's IP (it fails closed).
4. Traffic from the tunnel can't reach devices on the friend's LAN. If you turn
   on LAN mode (2.3), clients can reach them by IP.
5. Clients that aren't chosen are unaffected.
6. `scripts/tp.sh check` reaches the Pi from anywhere: through the VPS, or with
   `SSH_CLOUDFLARE` through Cloudflare even when the link is down.

## Quick start

Run everything from your computer (macOS or Linux), in this folder:

```sh
cp tunnel-pi.conf.example tunnel-pi.conf    # fill it in (see Settings)
scripts/tp.sh config                        # checks the settings
scripts/tp.sh base                          # 1.3: prepares the Pi
scripts/tp.sh all                           # 2.1: the link, the exit, the checks
scripts/tp.sh check                         # any time: PASS/FAIL on both ends
```

Before `tp.sh base` the Pi needs its OS (1.1), and, if you want the Cloudflare
emergency door, the dashboard steps (1.2). Run `scripts/tp.sh` with no arguments to
list every command.

## How it works

```
client ──WireGuard (WG_IF)──▶ VPS ──WireGuard (wgpi)──▶ Pi at friend's ──▶ internet
                                       ▲ the Pi dials out and keeps it open, so the friend's NAT doesn't matter
you ──ssh──▶ VPS ──(wgpi)──▶ Pi                          the everyday way in
you ──ssh──▶ Cloudflare ──▶ cloudflared on the Pi        optional: the emergency door
```

- **wgpi** is a WireGuard link the Pi dials out to your VPS and keeps open.
  Over it, the VPS reaches the Pi at `10.99.0.2`.
- **The exit:** the VPS sends the chosen clients' traffic over the link. The Pi
  sends it out to the internet under the friend's IP (NAT). `TPI_ROUTING` picks what
  does the routing on the VPS:
  - **`TPI_ROUTING=tpi`** (tunnel-pi): works with any WireGuard VPS. `vps.sh` adds
    the routing, a DNS redirect and a kill switch for the client addresses you
    list in `CLIENT_IPS`. Your WireGuard setup and client configs stay as they
    are.
  - **`TPI_ROUTING=wgt`**: for a VPS that runs wgt (the wireguard-tools in this
    repository). The Pi runs a small WireGuard "jump server" on the link, and
    the VPS adds it as an ordinary wgt jump. wgt then does the routing, fail
    closed and IPv6 blocking, and gives each device its own config.

  **Which one?** Use `tpi` unless your VPS already runs wgt. On a wgt VPS,
  `wgt` keeps all routing inside wgt, so `wgt client list` shows it. wgt
  hasn't been used with a jump reached through another tunnel before, so try
  it on a test server first if you can.
- **Admin access:** `tp.sh` reaches the Pi over ssh, by the first way that
  answers, and says which it used:
  1. `SSH_LAN`: on your home network, while you build.
  2. Through the VPS over the link (`ssh -J VPS_SSH user@10.99.0.2`): the
     everyday way after drop-off. Nothing extra to set up.
  3. `SSH_CLOUDFLARE`: SSH through a Cloudflare Tunnel, **the emergency door**.

  **Why Cloudflare, then?** The VPS way in only works while the link is up,
  and the link being down is exactly when you need to get in. The likely
  causes: the friend's network blocks UDP to `PI_PORT`, your VPS's IP changes,
  a change you made cuts the link, or the VPS itself is down. Cloudflare
  doesn't depend on the link, so with it you can still log in and fix the
  Pi. Without it, the fix is the friend power-cycling the Pi, or bringing it
  home. It's optional: it costs a domain (about $12 a year) and step 1.2.
  If you skip it, set `PI_PORT` to `443`, which networks rarely block.

**Why not carry WireGuard over Cloudflare?** WireGuard uses UDP only, and a
Cloudflare Tunnel carries only HTTP, WebSocket and TCP. Wrapping one in the
other needs a helper on every client, which phones can't run.

## What you need

- A Raspberry Pi 4 or 5 on wired ethernet, an SD card or (better) an SSD, and
  a power supply.
- A Linux VPS running the WireGuard server your clients already connect to,
  with `wg-quick`, `iptables` and systemd (Debian and Ubuntu have all three).
  `TPI_ROUTING=wgt` also needs wgt. If other people use that VPS, try this on a test
  server first: it adds routing next to theirs.
- On your computer: `ssh` to the Pi (Raspberry Pi OS gives its user
  passwordless `sudo`) and to the VPS (as root, or a user with passwordless
  `sudo`).
- Only for the emergency door (`SSH_CLOUDFLARE`): a domain whose DNS is on Cloudflare, a Cloudflare
  Zero Trust account, and `cloudflared` on your computer
  (macOS: `brew install cloudflared`).

**What the emergency door costs:** the tunnel is free, with no bandwidth charges. Zero
Trust's free plan covers 50 users, though signing up may ask for a payment
method. The only real cost is a domain, about $10–15 a year (or add a hostname
to one you already own). Client traffic never goes through Cloudflare: only
your SSH does, which keeps you well inside the free plan and its rules.

## Settings

`tunnel-pi.conf` holds them all, with a comment on each. `scripts/tp.sh config`
prints them, and stops if a required one is empty or still the example.

| Setting | | What to put |
|---|---|---|
| `SSH_LAN` | required | `user@name` of the Pi on your home network: the user and hostname you set in Raspberry Pi Imager (`pi@tunnel-pi.local`) |
| `SSH_CLOUDFLARE` | optional | the emergency door: an SSH hostname on your Cloudflare domain (`ssh.pi.example.com`). Empty: no Cloudflare, and you skip 1.2 |
| `CF_TOKEN_FILE` | with `SSH_CLOUDFLARE` | a file outside the repo, mode `600`, holding only the tunnel token from 1.2 |
| `WGT_SERVER` | optional | a VPS you already manage with `wgt-ctl`: its name in `bin/wgt-ctl list`. `tp.sh` then works through `wgt-ctl` (see below), and you can leave `VPS_SSH` and `VPS_HOST` as the example |
| `VPS_SSH` | required | how ssh reaches the VPS (`root@vps.example.com`). With `WGT_SERVER`: from `wgt-ctl`'s list |
| `VPS_HOST` | required | the VPS's public name or IP, which the Pi dials. With `WGT_SERVER`: the host in its ssh target, unless you set it |
| `PI_PORT` | required | a UDP port on the VPS that nothing listens on (check with `ss -ulpn`). wgt uses `51820`. Use `443` if it's free, above all without `SSH_CLOUDFLARE`: networks that filter UDP usually still allow it |
| `TPI_ROUTING` | required | `tpi` or `wgt` (see How it works) |
| `CLIENT_IPS` | `tpi` | the IPv4 `Address` of each client config that should exit through the Pi (on a wgt server: `wgt client list`) |
| `CLIENT_IP6S` | `tpi`, optional | those clients' IPv6 addresses, if they have any. They get blocked, because the link carries IPv4 only |
| `WG_IF` | `tpi` | the VPS's WireGuard interface your clients use: `wg show interfaces` on the VPS (usually `wg0`) |
| `WG_SUBNET` | `tpi`, optional | leave empty |
| `JUMP_CODE`, `JUMP_LABEL` | `wgt` | a code of 2–6 characters (`a-z`, `0-9`, `-`) that `wgt jump list` doesn't already show, and a label for it |
| `JUMP_LAN_CODE`, `JUMP_LAN_LABEL` | `wgt`, optional | a second jump in LAN mode (2.3) |

**With `WGT_SERVER`,** `tp.sh` uses `wgt-ctl` for everything on the VPS: its
server list, its host-key checks, and its handling of wgt commands. `tp.sh client`
then gives each device its fleet-wide number and puts its config straight into
`~/wg-tunnels/devices/<device>/config/`, as `wgt-ctl SERVER client add` always
does. Only `vps.sh` (the link, which wgt doesn't manage) is tunnel-pi's own, and
it too runs through `wgt-ctl SERVER ssh`. This needs `tunnel-pi/` to stay inside
the wireguard-tools checkout, next to `bin/wgt-ctl`.

In the steps below, a setting's name stands for its value: `ssh SSH_LAN`
means `ssh pi@tunnel-pi.local`.

## What's here

| File | Runs on | Does |
|---|---|---|
| `tunnel-pi.conf.example` | | the settings. Copy it to `tunnel-pi.conf`, which git ignores |
| `scripts/tp.sh` | your computer | drives everything over ssh (the VPS through `wgt-ctl`, with `WGT_SERVER`). It sends `pi.sh` and `vps.sh` with the settings, swaps public keys between the ends, and pipes the jump file from the Pi to the VPS |
| `scripts/pi.sh` | the Pi (root) | `base`, `link`, `jump`, `dns`, `lan-mode`, `check`, `undo` |
| `scripts/vps.sh` | the VPS (root) | `link`, `check`, `undo` |
| `scripts/tunnel-pi-exit` | the Pi | the exit's firewall rules: forward to the uplink, NAT, isolated or LAN mode. `tp.sh` installs it |

You can run any step again safely: files are rewritten only when they change,
and existing keys are kept. `TP_DRY_RUN=1 scripts/tp.sh <command>` shows what
would run, without changing anything. `pi.sh` and `vps.sh` can also run
without `tp.sh`, on their own machine: the top of each file says how.

## Reading the steps

Steps run in order. Each ends with a **Check**: don't go on until it passes.
A marker says where each step runs:

| Marker | Where |
|---|---|
| **[Computer]** | your machine, in this folder. Every `scripts/tp.sh` command runs here |
| **[Pi]** | a shell on the Pi, for looking around (`tp.sh` does the actual work): `ssh SSH_LAN` at home, `ssh -J VPS_SSH user@10.99.0.2` through the link, or `ssh SSH_CLOUDFLARE` |
| **[VPS]** | a root shell on the VPS |
| **[client]** | a device connected to the VPS |
| **[Browser]** | the Cloudflare dashboard. A person has to do this, because it needs a login |
| **[Hands]** | something physical: flashing, plugging in, a phone hotspot |

## For an AI agent following this

- **Ask before touching the VPS.** `tp.sh link`, `tp.sh exit`, `tp.sh client` and
  `tp.sh undo` change the VPS, and other people's traffic may run through it. Get
  the person's go-ahead for that server first, even for read-only commands.
  Suggest a test server if they have one. `tp.sh` also asks on the terminal; once
  the person has agreed for this VPS, `TP_YES=1` answers that question for
  you. When `tp.sh` reaches the Pi through the VPS, it only passes through: it
  doesn't change the VPS. Tell the person this happens, since it still
  connects to that server. Follow any rules the person or the project has about
  which servers you may use.
- **Some steps need a person:** everything marked **[Browser]** or
  **[Hands]**, the Cloudflare login in a browser on the first
  `ssh SSH_CLOUDFLARE`, and typing a Wi-Fi password at `tp.sh wifi add`'s
  prompt. Stop at each one, say exactly what to do, and wait.
- **Settings first.** Fill in `tunnel-pi.conf` with the person, then run
  `scripts/tp.sh config` until it says `settings look complete`.
- **Dry run first.** `TP_DRY_RUN=1 scripts/tp.sh all` shows every remote step
  without running it.
- **Keys stay where they are.** Never print or log a `*.key` file, a
  `PrivateKey` line, a jump file, the Cloudflare token or a Wi-Fi password. The scripts never do:
  the jump file goes from the Pi to the VPS through a pipe, and the token
  travels in ssh's input into a file only root can read.
- **A FAIL means stop.** Report the command, its output and the matching
  Troubleshooting row. Don't improvise a fix on the VPS.

---

# Phase 1: the Pi's base

## 1.1 Flash the Pi **[Hands]**

Use Raspberry Pi Imager to write **Raspberry Pi OS Lite (64-bit)**, Bookworm
or later (its NetworkManager is what lets the Pi switch networks on its own).
In its settings, set the hostname and user (they make up `SSH_LAN`), and turn
on **SSH with your public key only**, with passwords off. Skip Wi-Fi there:
`tp.sh wifi` handles it (3.3). Boot the Pi on your home network, on ethernet.

**Check [Computer]:** `ssh SSH_LAN true` succeeds without a password prompt.

## 1.2 Optional: the emergency door (Cloudflare) **[Browser]**

Skip this step if `SSH_CLOUDFLARE` is empty. It gives you SSH to the Pi that
doesn't need the WireGuard link (see How it works).

Go to Cloudflare Zero Trust (`one.dash.cloudflare.com`). Cloudflare renames
these menus from time to time, so follow the nearest match:

1. **Networks → Tunnels → Create a tunnel**, choose **Cloudflared**, and name
   it `tunnel-pi`. The install page shows a command containing a long
   **token**. Save the token alone (not the command) to `CF_TOKEN_FILE`:

   ```sh
   mkdir -p ~/.config/tunnel-pi
   install -m 600 /dev/stdin ~/.config/tunnel-pi/cloudflared.token   # paste the token, then Ctrl-D
   ```

   Don't run the dashboard's install command: `tp.sh base` does that part.
2. On the tunnel's **Public Hostname** tab (newer dashboards: **Published
   application routes**), add `SSH_CLOUDFLARE`, type `SSH`, URL `localhost:22`.
3. **Access → Applications → Add an application → Self-hosted** for
   `SSH_CLOUDFLARE`, with one **Allow** policy: *Emails* = your email.

**Check:** `CF_TOKEN_FILE` exists, and `scripts/tp.sh config` no longer prints a
note about it.

## 1.3 Prepare the Pi **[Computer]**

```sh
scripts/tp.sh base
ssh SSH_LAN sudo reboot          # once, so the hardware watchdog starts
scripts/tp.sh ssh-config         # only with SSH_CLOUDFLARE: paste what it prints into ~/.ssh/config
```

`tp.sh base` updates the Pi, and turns on automatic security updates and the
hardware watchdog. With `SSH_CLOUDFLARE`, it also installs cloudflared and runs it
as `tunnel-pi-cloudflared.service`, with the token in a file only root can
read.

**Check:** `tp.sh base` ends with `base done`, and after the reboot
`ssh SSH_LAN true` works again. With `SSH_CLOUDFLARE`: the dashboard shows the
tunnel as **Healthy**, and `ssh SSH_CLOUDFLARE` opens a browser login once
**[Browser]**, then gives you a shell on the Pi.

---

# Phase 2: WireGuard

The Pi stays on your home network for this phase. The away-from-home test is
in phase 3.

## 2.1 The link and the exit **[Computer]**

```sh
TP_DRY_RUN=1 scripts/tp.sh all  # see what it will do
scripts/tp.sh all               # asks before it changes the VPS
```

`tp.sh all` runs `tp.sh link`, `tp.sh exit` and `tp.sh check`. You can also run each one
on its own:

- **`tp.sh link`:**
  - On the Pi: installs WireGuard, turns on IP forwarding, and makes the Pi's
    key. Makes `wg-quick` keep retrying at boot, so a slow DHCP or DNS can't
    leave the link down.
  - On the VPS: makes its key, then writes and starts its end of `wgpi`, and
    opens `PI_PORT/udp` in ufw if ufw is on. With `TPI_ROUTING=tpi`, it also adds
    the routing for `CLIENT_IPS` and the kill switch.
  - Then it writes and starts the Pi's end. `tp.sh` swaps the public keys for you.
- **`tp.sh exit`:**
  - `TPI_ROUTING=tpi`: installs a DNS resolver (unbound) on the Pi to answer the
    clients' lookups.
  - `TPI_ROUTING=wgt`: makes the jump server on the Pi and pipes its jump file
    straight into `wgt jump add JUMP_CODE` on the VPS, then deletes the file.
    With `JUMP_LAN_CODE` set, it does the same for a second jump in LAN mode
    (2.3).
- **`tp.sh check`:** PASS/FAIL lines for both ends.

**Check:** `tp.sh check` ends with `all checks passed`. With `TPI_ROUTING=wgt`, the
`wgt jump add` output ended with `jump 'JUMP_CODE' works: ip=<your home IP>`.

**Changing `CLIENT_IPS` later** (`TPI_ROUTING=tpi`): edit it and run
`scripts/tp.sh link` again.

**If the VPS runs wgt:** wgt doesn't manage `wgpi`, and it leaves alone the
rules it didn't make. Its backups include all of `/etc/wireguard`, `wgpi`
included, but `wgt restore` and `wgt-ctl move` don't start the tunnel-pi parts:
run `scripts/tp.sh link` again afterwards. With `TPI_ROUTING=tpi`, don't also give a
client in `CLIENT_IPS` a wgt jump: tunnel-pi's routing comes first, so the
client would never use the jump.

## 2.2 Give devices a config (`TPI_ROUTING=wgt` only) **[Computer]**

```sh
scripts/tp.sh client phone       # makes the config JUMP_CODE.phone
scripts/tp.sh client laptop lan  # with JUMP_LAN_CODE: JUMP_LAN_CODE.laptop, in LAN mode
```

This runs `wgt client add <device> --exit <code> --warp off` on the VPS. With
`WGT_SERVER` it goes through `wgt-ctl`, so the device keeps its fleet-wide
number and the config lands in `~/wg-tunnels/devices/<device>/config/`. To show
it as a QR code: `bin/wgt-ctl WGT_SERVER client show <code>.<device> --qr`
(without `WGT_SERVER`, `wgt client show ... --qr` on the VPS).

`--warp off` matters. Without it, wgt also makes a `warp.` config that puts
Cloudflare WARP inside the jump, and sites would see a Cloudflare IP instead of
the friend's. wgt blocks traffic when the jump is down, and blocks IPv6
through it. DNS goes to the config's `DNS` server (by default `1.1.1.1`)
through the jump, so lookups also leave from the friend's IP.

With `TPI_ROUTING=tpi` there's no `tp.sh client`: every config listed in `CLIENT_IPS`
already goes through the Pi.

## 2.3 Optional: LAN mode (reaching the friend's LAN)

By default the Pi is **isolated**: clients get the friend's internet
connection, and nothing on the friend's LAN. In **LAN mode**, clients can also
reach devices on that LAN by IP: a NAS, a printer, the router's admin page.
**Ask the friend first:** this lets your devices into their home network.

What LAN mode is, and isn't:

- **One way.** Your clients can open connections to LAN devices. LAN devices
  can't open connections to your clients, and they see every connection as
  coming from the Pi.
- **By IP only.** Discovery (AirPlay, Chromecast, the Finder's Network list)
  doesn't reach across, and names like `nas.lan` don't resolve. Connect by
  address instead: `smb://192.168.1.20`, `http://192.168.1.1`.
- **Not membership.** A client doesn't get an address on the friend's LAN.

**Turn it on or off [Computer]:**

```sh
scripts/tp.sh lan-mode lan       # on: wgpi with TPI_ROUTING=tpi, wgjump with TPI_ROUTING=wgt
scripts/tp.sh lan-mode isolated  # off again
```

This restarts that one interface. cloudflared doesn't depend on it, so it's
safe to do after drop-off. "The LAN" means the subnet the Pi is on when the
interface starts, so at home it's your own LAN.

**For some clients only** (`TPI_ROUTING=wgt`): the mode applies to every client of
an interface, because the Pi sees them all as one address. To choose per
device, keep `wgjump` isolated and set `JUMP_LAN_CODE` (and `JUMP_LAN_LABEL`).
`tp.sh exit` then adds a second jump, `wgjumplan`, in LAN mode, and each device
picks one by its config: `home.phone` (isolated) or `homelan.laptop` (LAN).

**Check [Pi]:** `journalctl -u wg-quick@wgjump -n 20` (or `@wgpi`) shows
`tunnel-pi-exit: ... LAN <subnet> reachable`.

**Overlapping subnets break it.** The friend's LAN (often `192.168.0.0/24` or
`192.168.1.0/24`) must not overlap:

- **the client's own network.** A laptop on a café network that also uses
  `192.168.1.0/24` sends those addresses to the café, not the tunnel. Try
  another network, such as a phone hotspot.
- **any subnet on the VPS's `WG_IF`.**
- **the link's addresses,** `10.99.0.0/30` and `10.98.0.0/29`.

---

# Phase 3: prove it, then drop it off

## 3.1 The full test, on a phone hotspot **[Hands]**, then **[Computer]**/**[client]**

Move the Pi onto a **phone hotspot**. Like the friend's network, it's a NAT you
don't control, with no open ports. Any of these works:

- a travel router joined to the hotspot, with the Pi's ethernet plugged into
  it (closest to the friend's setup);
- a USB-to-ethernet adapter tethered to the phone;
- the hotspot's Wi-Fi: while the Pi is still on ethernet, run
  `scripts/tp.sh wifi add "<hotspot name>"`, then unplug the ethernet. 3.3 removes
  it again.

No reboot is needed: when the Pi's network changes, the exit rules follow it
on their own (a NetworkManager hook that `tp.sh link` installs). Keep your computer and the client on a **different** network from the hotspot. The
"Pi's public IP" below is the hotspot's: `tp.sh check` prints it.

| # | Test | Pass |
|---|---|---|
| 1 | **[Computer]** `scripts/tp.sh check` | `all checks passed`, and the first line says `via the link` (not `SSH_LAN`) |
| 2 | **[client]** (a Pi-exit config) `curl https://ifconfig.me` | the Pi's public IP |
| 3 | **[client]** a DNS leak test site (for example dnsleaktest.com) | `tpi`: only the Pi's ISP. `wgt`: Cloudflare's resolvers, reached from the Pi's IP |
| 4 | **[client]** `curl -6 -m 10 https://ifconfig.me` | fails (IPv6 blocked) |
| 5 | **[Pi]** `sudo systemctl stop wg-quick@wgpi`, then **[client]** `curl -m 10 https://ifconfig.me` | times out. It must **not** print the VPS's IP |
| 6 | only with `SSH_CLOUDFLARE`, wgpi still stopped: **[Computer]** `scripts/tp.sh check` | its first line says `via SSH_CLOUDFLARE`: the emergency door works |
| 7 | **[Pi]** `sudo systemctl start wg-quick@wgpi` (without `SSH_CLOUDFLARE`, test 5 cut you off: power-cycle the Pi instead) | `tp.sh check` passes again |
| 8 | `tpi` only: **[VPS]** `systemctl stop wg-quick@wgpi`, then **[client]** `curl -m 10 https://ifconfig.me`, then **[VPS]** `systemctl start wg-quick@wgpi` | times out (the kill switch). It must **not** print the VPS's IP |
| 9 | **[client]** `ping -c2` the Pi's router (on the Pi, `ip route` shows it after `default via`) | isolated: no reply. LAN mode: replies |
| 10 | **[client]** a config that is **not** a Pi exit: `curl https://ifconfig.me` | unchanged: the same IP as before |

## 3.2 Survive a power cut **[Hands]**

Unplug the Pi and plug it back in, as will happen at the friend's.

**Check [Computer]:** about a minute later, with no one touching the Pi,
`scripts/tp.sh check` passes and test 2 passes again. A Pi that passes this is
ready: drop-off is the same power-up on a different network.

## 3.3 Ethernet or Wi-Fi at the friend's **[Computer]**

The Pi uses whatever network it has: ethernet when it's plugged in, else any
saved Wi-Fi in range. Decide what it should have at the friend's, and set it
now, while it's still with you:

```sh
scripts/tp.sh wifi                           # the saved Wi-Fi networks, and the uplink now
scripts/tp.sh wifi remove all                # ethernet only (the usual choice)
scripts/tp.sh wifi add "<friend's network>"  # or: their Wi-Fi too (asks for the password)
```

- **Ethernet only** is the most reliable, and the friend configures nothing.
  `remove all` also clears a Wi-Fi network set in Raspberry Pi Imager and the
  hotspot from 3.1. It won't remove the network the Pi is using right now:
  plug in ethernet first.
- **The friend's Wi-Fi** needs their password, and works when a cable can't
  reach. With both, ethernet wins whenever it's plugged in, and the Pi moves
  between them on its own.

**Check:** `scripts/tp.sh check` shows `INFO  uplink: eth0. Saved Wi-Fi: ...`
with what you expect.

## 3.4 Harden

- **The SD card:** power cuts wear it out. The best fix is an SSD (USB boot).
  With an SD card, `sudo raspi-config` → Performance → Overlay file system
  protects it, but then nothing is saved across reboots, including the
  automatic updates. Each change, update included, means turning the overlay
  off, rebooting, changing, and turning it back on.
- **Updates:** `tp.sh base` turned on `unattended-upgrades`, which installs
  security fixes while the file system is writable.
- After any change here, repeat 3.2.

## 3.5 Drop-off **[Hands]**

The friend plugs in ethernet and power (or just power, with their Wi-Fi saved
in 3.3). That's all.

**Check [Computer], within a few minutes:** `scripts/tp.sh check` passes and
shows the friend's IP, and a Pi-exit client's `curl https://ifconfig.me` shows
the same. Running tests 2–10 of 3.1 again is a good idea.

If it doesn't come up, ask the friend to check that the ethernet lights are on,
then to unplug the power and plug it back in. If it still doesn't come up,
their network blocks something the hotspot didn't, usually outbound UDP to
`PI_PORT`. If `ssh SSH_CLOUDFLARE` works but `tp.sh check` shows no `wgpi` handshake,
that's the cause: set `PI_PORT="443"` (if it's free on the VPS) and run
`scripts/tp.sh link` again, which updates both ends. Without `SSH_CLOUDFLARE` you
can't reach the Pi to change it, so take it home, or choose `443` from the
start.

---

## Undo **[Computer]**

```sh
scripts/tp.sh undo       # removes the VPS's tunnel-pi parts, then the Pi's WireGuard parts
scripts/tp.sh undo all   # the same, plus cloudflared on the Pi
```

`tp.sh undo` removes the link, so it can't run through it: it uses `SSH_LAN`
or, away from home, `SSH_CLOUDFLARE`.

With `TPI_ROUTING=wgt`, remove the devices' configs first: wgt won't remove a jump
that configs still use. On the VPS, run `wgt client remove <code>.<device>`
for each one `wgt client list` shows. `tp.sh undo` then removes the jump(s),
`wgpi`, the kill switch (`TPI_ROUTING=tpi`) and the ufw rule for `PI_PORT`. After
`undo all`, delete the tunnel and the Access application in the dashboard
**[Browser]**.

**Check [VPS]:** `ip rule` shows nothing at `900`/`901`, `wgt jump list` no
longer shows the code(s), and the clients are back on their usual routes.

## Troubleshooting

| Symptom | Look at |
|---|---|
| `tp.sh` says `via the link` at home | `SSH_LAN` didn't answer: is the Pi on, and does `ssh SSH_LAN true` work without a prompt? |
| `tp.sh` says `via SSH_CLOUDFLARE` | the link is down, and you're in through the emergency door: run `scripts/tp.sh check` and see the rows below |
| `tp.sh` can't reach the Pi at all | without `SSH_CLOUDFLARE`: the friend power-cycles the Pi. With it: is the tunnel Healthy in the dashboard? Does the Access policy include your email? Is the `tp.sh ssh-config` entry in `~/.ssh/config`? |
| `tp.sh` stops at "This changes the VPS ... Continue?" without a terminal | it needs an answer: run it in a terminal, or set `TP_YES=1` once the person has agreed |
| `vps.sh: 10.99.0.0/30 is already used` or `port ... in use` | pick another `PI_PORT`, or free the link's addresses on the VPS |
| FAIL `wgpi has no recent handshake` | **[VPS]** is `PI_PORT/udp` open (`ss -ulpn`, ufw, the provider's firewall)? **[Pi]** does `VPS_HOST` resolve? Then run `scripts/tp.sh link` again, which swaps the keys again |
| **[Pi]** `wg-quick@wgpi` keeps restarting | `journalctl -u wg-quick@wgpi -n 20`: `no default route yet` means no network, and a failed name lookup means `VPS_HOST` doesn't resolve |
| `wgt jump add` fails | **[VPS]** `ping 10.99.0.2` (the link first), then `wg show px-JUMP_CODE`. **[Pi]** `sudo wg show wgjump`. Fix it, then `TP_RENEW=1 scripts/tp.sh exit` makes new jump keys and tries again |
| `the Pi's jump server exists but the VPS has no jump` | the jump was removed on the VPS: `TP_RENEW=1 scripts/tp.sh exit` |
| `tpi`: handshake, but the client has no internet | **[VPS]** `ip rule` (is 900 first?), `ip route show table 260`. **[Pi]** `sudo iptables -t nat -L -v`, `journalctl -u wg-quick@wgpi` (the `tunnel-pi-exit:` line names the uplink) |
| `tpi`: sites load but names don't resolve | **[Pi]** `systemctl status unbound`. **[VPS]** `iptables -t nat -L PREROUTING -v` |
| Some sites hang, usually large pages | MTU. wgt's client configs use 1280, which fits through both tunnels. A config with a larger `MTU` needs lowering |
| A client shows the VPS's IP | `tpi`: is its address in `CLIENT_IPS`? `systemctl status tunnel-pi-killswitch`. `wgt`: is the config named `<code>.<device>`? |
| Traffic stops after the Pi changes networks | the exit rules should follow on their own: **[Pi]** `journalctl -t tunnel-pi-exit -n 20` shows each move. If nothing moved, reboot the Pi |

## What the scripts change

**On the Pi:** `/etc/wireguard/wgpi.*`, `wgjump*` and `wgjumplan*`, and
briefly `jump-for-vps*.conf`. Also `/usr/local/sbin/tunnel-pi-exit`,
`/etc/sysctl.d/99-tunnel-pi.conf`,
`/etc/systemd/system/wg-quick@.service.d/tunnel-pi-retry.conf`,
`/etc/NetworkManager/dispatcher.d/90-tunnel-pi` (moves the exit rules when the
network changes), and with `TPI_ROUTING=tpi` `/etc/unbound/unbound.conf.d/tunnel-pi.conf`.
`tp.sh wifi add` writes `/etc/NetworkManager/system-connections/tunnel-pi-*.nmconnection`
(mode 600, with the password), and `tp.sh wifi remove` deletes Wi-Fi connections.
`tp.sh base` turns on automatic updates and sets `RuntimeWatchdogSec=15` in
`/etc/systemd/system.conf`. With `SSH_CLOUDFLARE` it also adds Cloudflare's apt
repository, `/etc/systemd/system/tunnel-pi-cloudflared.service` and
`/etc/cloudflared/tunnel-pi.env` (the token, mode 600).

**On the VPS:** `/etc/wireguard/wgpi.*` and a ufw rule for `PI_PORT/udp`. With
`TPI_ROUTING=tpi`, also `/usr/local/sbin/tunnel-pi-killswitch` and
`/etc/systemd/system/tunnel-pi-killswitch.service`. With `TPI_ROUTING=wgt`, wgt's own
`jump add` and `client add`. Nothing changes `WG_IF`'s configuration, the
client configs, or wgt's files.

**Fixed values.** They avoid wgt's own numbers and common setups, and
`vps.sh` stops if the VPS already uses the link's addresses or `PI_PORT`.

| Value | What it is |
|---|---|
| `10.99.0.0/30` | the link: VPS `10.99.0.1`, Pi `10.99.0.2` |
| `10.98.0.0/30`, UDP `51822` | `wgt`: the jump server (`10.98.0.1` on the Pi, `10.98.0.2` for the VPS). The LAN-mode jump uses `10.98.0.4/30` and `51823` |
| table `260`, rule priorities `900`/`901` | `tpi`: the routing table, and the rules that send the listed clients to it (wgt uses `100`, `200`–`250` and `9300`–`10000`) |
| interfaces `wgpi`, `wgjump`, `wgjumplan` | the link and the jump servers |

## Limits and care

- **Speed:** your downloads are the friend's uploads, so their upload speed
  caps yours. Every request also takes the longer path client → VPS → Pi.
- **The friend's IP changes:** that's fine. The Pi dials out again, and the
  VPS learns the new address.
- **Your VPS's IP changes:** `wg-quick` looks up `VPS_HOST` only when it
  starts, so the link stays down until it restarts. With `SSH_CLOUDFLARE`, run
  `ssh SSH_CLOUDFLARE sudo systemctl restart wg-quick@wgpi`. Without it, the friend
  power-cycles the Pi.
- **Other things that give you away:** the IP, DNS and IPv6 are covered.
  WebRTC, the browser's time zone and language, phone location services and
  your account history aren't. Strict services (banks, streaming) check these.
- **The friend:** to their ISP, all of this traffic belongs to their household.
  Let only clients you trust use this exit, and tell the friend what the box
  does.
