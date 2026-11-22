# tunnel-pi: a Raspberry Pi as a residential exit

## Goal

Put a Raspberry Pi on a friend's home network so that chosen WireGuard clients
reach the internet **from the friend's home IP**. The friend only plugs in power
and an ethernet cable. They change no router settings and open no ports.

**Everything is built and tested at home.** At drop-off the Pi is finished:
it gets an address by DHCP, dials out, and every connection comes up on its
own. Nothing is set up after drop-off. If something breaks while you build,
the Pi is right there on your desk.

The work comes in three phases, all at home:

| Phase | What |
|---|---|
| **1. Base and admin** | the OS, updates, a watchdog, and remote SSH over cloudflared |
| **2. WireGuard** | the link to your VPS, then the exit: **by hand** (2B) **or with wgt** (2C) |
| **3. Prove and drop off** | the full test on a phone hotspot (it copies the friend's network), a reboot test, hardening, then the hand-off |

After drop-off, `ssh ADMIN_HOST` is for maintenance only: updates, checks, and
fixes if something stops working.

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
client ──WG (wg0)──▶ VPS ──WG (wgpi)──▶ Pi at friend's ──eth0──▶ internet
                              ▲ the Pi dials out (keepalive), so the friend's NAT/CGNAT doesn't matter
you ──ssh via cloudflared──▶ Cloudflare ──▶ cloudflared on the Pi ──▶ sshd
```

- **cloudflared** gives you SSH to the Pi. It carries admin traffic only.
- **wgpi** is a WireGuard link that the Pi dials out to the VPS and keeps open.
  Through it, the VPS can reach the Pi at `10.99.0.2`.
- **The exit:** the VPS sends the chosen clients' traffic over the link, and
  the Pi NATs it out of `eth0` with the friend's IP. There are two ways to do
  this:
  - **2B by hand:** the VPS's policy routing, DNS redirect and kill switch are
    written by hand.
  - **2C with wgt:** the Pi also runs a small WireGuard "jump server" on its
    link address. The VPS adds it as an ordinary wgt **jump**, and wgt does the
    rest: per-client routing, fail-closed, IPv6, and configs named
    `<code>.<device>`.

**Which one?** On a wgt server, choose **2C**. It's less hand-written routing,
and the result shows in `wgt client list`. It is new ground, though: a jump
reached over another tunnel hasn't been tried with wgt yet, so prove it on
bossk. Choose 2B for a VPS without wgt.

**Why not carry WireGuard over cloudflared?** WireGuard only uses UDP, and a
Cloudflare Tunnel only carries HTTP, WebSocket and TCP. Wrapping WireGuard to
fit means running a helper on every client, which phones can't do.

## What you need

- A Raspberry Pi 4 or 5 on wired ethernet, an SD card or SSD, and a power supply.
- A VPS your clients already connect to (a wgt server for 2C). **Build and
  test on bossk.** This adds routing next to wgt's on a server carrying live
  traffic.
- A domain whose DNS is on Cloudflare, and a Cloudflare Zero Trust account.

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

## Values to fill in

Settle these before you start. Every command below uses them as written, so
replace them everywhere.

| Placeholder | Example | Used in | How to find it |
|---|---|---|---|
| `ADMIN_HOST` | `ssh.pi.example.com` | all | the admin hostname on your Cloudflare domain |
| `ADMIN_EMAIL` | `you@example.com` | all | the email Cloudflare Access lets in |
| `PI_USER` | `pi` | all | the user you set in Raspberry Pi Imager |
| `VPS_HOST` | `bossk.example.com` | 2 | the VPS's public name or IP (the `Endpoint` host in any client config) |
| `PI_PORT` | `51821` | 2 | a free UDP port on the VPS (`ss -ulpn` shows nothing on it). wgt uses `51820` |
| `WG_SUBNET` | `10.214.237.0/24` | 2B | the VPS's client subnet: `WG_SUBNET` in wgt's state, or any client config's `Address` as a /24 |
| `CLIENT_IP` | `10.214.237.5` | 2B | the IPv4 `Address` of the client config that should exit through the Pi (`wgt client list`) |
| `CLIENT_IP6` | `fd12:3456::5` | 2B | that config's IPv6 `Address`, if it has one |
| `JUMP_CODE` | `home` | 2C | the jump's code: 2–6 characters of `a-z`, `0-9`, `-` |
| `JUMP_LABEL` | `Friend's house, Chicago` | 2C | its name in client lists |
| `DEVICE` | `phone` | 2C | the device that gets a config through the Pi |

These are fixed, chosen to stay clear of wgt (`wgt/const.py`). Change them only
after checking wgt's own values:

| Fixed value | What it is |
|---|---|
| `10.99.0.0/30` | the link: VPS `10.99.0.1`, Pi `10.99.0.2`. It must be unused on the VPS (`ip route`) |
| `10.98.0.0/30`, UDP `51822` | 2C only: the jump server on the Pi (`10.98.0.1`) and the VPS's jump address (`10.98.0.2`). A second, LAN-mode jump (2D.2) uses `10.98.0.4/30` and `51823` |
| table `260`, priorities `900`/`901` | 2B only: wgt uses tables `100` and `200`–`250`, and priorities `9300`–`10000` |
| interface names `wgpi`, `wgjump` | wgt's are `wg0`, `warp`, `px-*`, `wx-*` |

## Where each step runs

| Marker | Where |
|---|---|
| **[Pi]** | a shell on the Pi: over your LAN at first, then `ssh ADMIN_HOST` once step 1.2 is done (use whichever works) |
| **[VPS]** | a root shell on the VPS |
| **[Mac]** | your own machine |
| **[client]** | a device connected through the VPS |
| **[Browser]** | the Cloudflare dashboard. A person does this: it needs a login |

Steps run in order. Each one ends with a **Check**. Don't go on until it passes.

---

# Phase 1: base and admin

This phase gives you a Pi you can reach through Cloudflare from any network.
Nothing in it touches WireGuard or any VPS.

## 1.1 Flash and update

**[Mac]** Use Raspberry Pi Imager to write **Raspberry Pi OS Lite (64-bit)**.
In its settings, set the hostname (`tunnel-pi`), the user `PI_USER`, and **SSH
with your public key only**, with passwords off. Boot the Pi on your home
network and SSH in on the LAN.

**[Pi]**

```sh
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades      # answer Yes

# Hardware watchdog: a hung Pi reboots itself
sudo sed -i 's/^#\?RuntimeWatchdogSec=.*/RuntimeWatchdogSec=15/' /etc/systemd/system.conf
```

**Check [Pi]:** `systemctl show -p RuntimeWatchdogUSec` shows a value other
than `infinity` (after the next reboot).

## 1.2 Cloudflare Tunnel and Access

**[Browser]** In Cloudflare Zero Trust:

1. Go to **Networks → Tunnels → Create a tunnel**, choose **Cloudflared**, and
   name it `tunnel-pi`. Choose **Debian / arm64**. The page shows install
   commands and a **token**.
2. On the tunnel's **Public Hostname** tab, add `ADMIN_HOST` → type `SSH`,
   URL `localhost:22`.
3. Go to **Access → Applications → Add an application → Self-hosted** for
   `ADMIN_HOST`, with one **Allow** policy: *Emails* = `ADMIN_EMAIL`.

**[Pi]** Run the install commands the page showed. They come down to adding
Cloudflare's apt repository, then:

```sh
sudo apt install -y cloudflared
sudo cloudflared service install <TOKEN>      # the token from step 1
```

**[Mac]**

```sh
brew install cloudflared
cat >> ~/.ssh/config <<'EOF'

Host ADMIN_HOST
    User PI_USER
    ProxyCommand /opt/homebrew/bin/cloudflared access ssh --hostname %h
EOF
```

**Check:** the dashboard shows the tunnel as **Healthy**, and **[Mac]**
`ssh ADMIN_HOST` opens a browser login once, then gives a shell on the Pi.

---

# Phase 2: WireGuard

The Pi is still on your home network. The away-from-home test comes in
phase 3.

## 2A. The link (needed for both 2B and 2C)

### 2A.1 Packages and forwarding

**[Pi]**

```sh
sudo apt install -y wireguard iptables
echo 'net.ipv4.ip_forward=1' | sudo tee /etc/sysctl.d/99-tunnel-pi.conf
sudo sysctl --system
```

### 2A.2 Keys for the link

Each private key is made on its own machine and never leaves it. Only the
public keys are copied across.

**[Pi]** and **[VPS]**, the same command on each:

```sh
sudo sh -c 'umask 077; wg genkey > /etc/wireguard/wgpi.key; wg pubkey < /etc/wireguard/wgpi.key > /etc/wireguard/wgpi.pub'
sudo cat /etc/wireguard/wgpi.pub
```

Call the Pi's output `PI_PUBKEY` and the VPS's `VPS_PUBKEY`.

### 2A.3 The exit rules script (Pi)

Both 2B and 2C use this script to send traffic arriving on a WireGuard
interface out of `eth0`. Its third word is the **LAN mode**:

- **`isolated`** (the default): the internet only. Nothing on the friend's
  LAN can be reached.
- **`lan`**: also the friend's LAN, meaning the subnet `eth0` is on (see 2D).

**[Pi]** Create `/usr/local/sbin/tunnel-pi-exit` with mode `755`:

```sh
#!/bin/sh
# tunnel-pi-exit up <interface> [isolated|lan] | down <interface>
# Traffic arriving on <interface> leaves through eth0 with the friend's IP.
# isolated: never into their LAN (or any private range). lan: their LAN
# (eth0's subnet) too, still no other private range.
set -e
IF="$2"; MODE="${3:-isolated}"
[ -n "$IF" ] || { echo "usage: $0 up <interface> [isolated|lan] | down <interface>" >&2; exit 2; }
PRIVATE=10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,169.254.0.0/16,100.64.0.0/10
SAVED="/run/tunnel-pi-lan.$IF"      # the LAN let in at up, so down removes the same rule
case "$1" in
  up)
    iptables -I FORWARD -i "$IF" -d "$PRIVATE" -j DROP
    if [ "$MODE" = lan ]; then
      LAN=$(ip -4 -o addr show dev eth0 | awk '{print $4; exit}')   # e.g. 192.168.1.50/24
      if [ -n "$LAN" ]; then
        iptables -I FORWARD -i "$IF" -o eth0 -d "$LAN" -j ACCEPT    # above the DROP
        echo "$LAN" > "$SAVED"
      else
        echo "tunnel-pi-exit: eth0 has no IPv4 address yet; $IF stays isolated" >&2
      fi
    elif [ "$MODE" != isolated ]; then
      echo "tunnel-pi-exit: mode must be isolated or lan" >&2; exit 2
    fi
    iptables -A FORWARD -i "$IF" -o eth0 -j ACCEPT
    iptables -A FORWARD -i eth0 -o "$IF" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
    iptables -t nat -C POSTROUTING -o eth0 -j MASQUERADE 2>/dev/null \
      || iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
    ;;
  down)
    if [ -r "$SAVED" ]; then
      iptables -D FORWARD -i "$IF" -o eth0 -d "$(cat "$SAVED")" -j ACCEPT || true
      rm -f "$SAVED"
    fi
    iptables -D FORWARD -i "$IF" -d "$PRIVATE" -j DROP || true
    iptables -D FORWARD -i "$IF" -o eth0 -j ACCEPT || true
    iptables -D FORWARD -i eth0 -o "$IF" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT || true
    ;;
  *) echo "usage: $0 up <interface> [isolated|lan] | down <interface>" >&2; exit 2 ;;
esac
```

### 2A.4 The Pi's link: `/etc/wireguard/wgpi.conf` (mode `600`)

**[Pi]**

```ini
[Interface]
Address = 10.99.0.2/30
MTU = 1420
PostUp = wg set %i private-key /etc/wireguard/wgpi.key
# 2B adds two lines here (see 2B.3)

[Peer]
# the VPS
PublicKey = VPS_PUBKEY
Endpoint = VPS_HOST:PI_PORT
AllowedIPs = 10.99.0.1/32
PersistentKeepalive = 25
```

The keepalive holds the NAT mapping on the friend's router open, so the VPS can
always reach the Pi.

### 2A.5 The VPS's link: `/etc/wireguard/wgpi.conf` (mode `600`)

**[VPS]**

```ini
[Interface]
Address = 10.99.0.1/30
ListenPort = PI_PORT
MTU = 1420
PostUp = wg set %i private-key /etc/wireguard/wgpi.key

[Peer]
# the Pi
PublicKey = PI_PUBKEY
AllowedIPs = 10.99.0.2/32
```

2B replaces this file with a longer one (2B.1). 2C uses it as it is.

### 2A.6 Start the link

**[VPS]**

```sh
ufw allow PI_PORT/udp     # if the VPS uses ufw; otherwise open PI_PORT/udp in your firewall
systemctl enable --now wg-quick@wgpi
```

**[Pi]**

```sh
sudo systemctl enable --now wg-quick@wgpi
```

**Check:**

- **[Pi]** `sudo wg show wgpi` shows a `latest handshake` a few seconds old,
  and `ping -c3 10.99.0.1` works.
- **[VPS]** `ping -c3 10.99.0.2` works. The VPS can reach the Pi, even though
  the Pi is behind NAT.
- **[Pi]** `ssh ADMIN_HOST` still works from your Mac. Nothing in cloudflared
  changed.

Now do **either 2B or 2C**, not both.

---

## 2B. The exit, by hand

### 2B.1 The VPS's routing: replace `/etc/wireguard/wgpi.conf`

**[VPS]**

```ini
[Interface]
Address = 10.99.0.1/30
ListenPort = PI_PORT
MTU = 1420
# Table = off: no routes from AllowedIPs; the rules below choose who uses the link
Table = off
PostUp = wg set %i private-key /etc/wireguard/wgpi.key

# Table 260: the Pi for everything, except the VPS's own clients, which stay on wg0
PostUp = ip route replace WG_SUBNET dev wg0 table 260
PostUp = ip route replace default dev %i table 260
PostDown = ip route flush table 260

# Which clients use it (one rule pair per client; a CIDR works too)
PostUp = ip rule add iif wg0 from CLIENT_IP lookup 260 priority 900
PostDown = ip rule del iif wg0 from CLIENT_IP lookup 260 priority 900

# Forward into the link and NAT onto it (the Pi sees only 10.99.0.1)
PostUp = iptables -I FORWARD -i wg0 -o %i -j ACCEPT
PostUp = iptables -I FORWARD -i %i -o wg0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
PostUp = iptables -t nat -A POSTROUTING -o %i -j MASQUERADE
PostDown = iptables -D FORWARD -i wg0 -o %i -j ACCEPT
PostDown = iptables -D FORWARD -i %i -o wg0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
PostDown = iptables -t nat -D POSTROUTING -o %i -j MASQUERADE

# Their DNS, whatever server the client asks, is answered by the Pi
PostUp = iptables -t nat -I PREROUTING -i wg0 -s CLIENT_IP -p udp --dport 53 -j DNAT --to-destination 10.99.0.2
PostUp = iptables -t nat -I PREROUTING -i wg0 -s CLIENT_IP -p tcp --dport 53 -j DNAT --to-destination 10.99.0.2
PostDown = iptables -t nat -D PREROUTING -i wg0 -s CLIENT_IP -p udp --dport 53 -j DNAT --to-destination 10.99.0.2
PostDown = iptables -t nat -D PREROUTING -i wg0 -s CLIENT_IP -p tcp --dport 53 -j DNAT --to-destination 10.99.0.2

[Peer]
# the Pi
PublicKey = PI_PUBKEY
AllowedIPs = 0.0.0.0/0
```

### 2B.2 The kill switch: `/etc/systemd/system/tunnel-pi-killswitch.service`

**[VPS]** If `wgpi` is down, these clients must lose their connection, not
quietly fall back to the VPS's own IP. These rules live outside `wgpi`, so they
stay in place when it stops:

```ini
[Unit]
Description=tunnel-pi: Pi-exit clients never fall back to the VPS's IP
After=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'ip rule del iif wg0 from CLIENT_IP unreachable priority 901 2>/dev/null; ip rule add iif wg0 from CLIENT_IP unreachable priority 901'
# The link is IPv4 only: block the client's IPv6 so it can't leak the VPS's address
ExecStart=/bin/sh -c 'ip6tables -C FORWARD -i wg0 -s CLIENT_IP6 -j REJECT 2>/dev/null || ip6tables -I FORWARD -i wg0 -s CLIENT_IP6 -j REJECT'
ExecStop=/bin/sh -c 'ip rule del iif wg0 from CLIENT_IP unreachable priority 901; ip6tables -D FORWARD -i wg0 -s CLIENT_IP6 -j REJECT'

[Install]
WantedBy=multi-user.target
```

If the client has no IPv6 address, leave out the `ip6tables` parts.

```sh
systemctl daemon-reload
systemctl enable --now tunnel-pi-killswitch.service
systemctl restart wg-quick@wgpi
```

### 2B.3 The Pi's exit and DNS

**[Pi]** Add these two lines to the `[Interface]` section of
`/etc/wireguard/wgpi.conf`:

```ini
PostUp = /usr/local/sbin/tunnel-pi-exit up %i isolated
PostDown = /usr/local/sbin/tunnel-pi-exit down %i
```

Set up a recursive DNS resolver, so lookups come from the friend's ISP:

```sh
sudo apt install -y unbound
sudo tee /etc/unbound/unbound.conf.d/tunnel-pi.conf <<'EOF'
server:
    interface: 127.0.0.1
    interface: 10.99.0.2
    ip-freebind: yes
    access-control: 127.0.0.0/8 allow
    access-control: 10.99.0.0/30 allow
EOF
sudo unbound-checkconf
sudo systemctl restart unbound wg-quick@wgpi
```

(`ip-freebind` lets unbound start before `wgpi` has its address.)

**Check:**

- **[VPS]** `ip rule` shows `900: ... lookup 260` and `901: ... unreachable`,
  both before every wgt rule (`9300` and up). `ip route show table 260` shows
  `default dev wgpi` and `WG_SUBNET dev wg0`.
- **[Pi]** `dig @10.99.0.2 example.com +short` returns an address.
- **[client]** `curl https://ifconfig.me` shows the IP of the Pi's network.

> **Alongside wgt.** wgt doesn't manage any of this, and leaves rules it didn't
> make alone. Check `ip rule` after any `wgt setup`. Don't also give this
> client a jump: priority 900 wins, so the jump would never be used.

Then 2D if the clients should reach the friend's LAN; otherwise go to phase 3.

---

## 2C. The exit, with wgt (a jump)

The Pi runs a second WireGuard interface, `wgjump`, as a "server" on its link
address. The VPS adds it as a wgt jump, just as it would a Proton file, with
`Endpoint = 10.99.0.2:51822`. wgt then brings the jump up on demand, sends
configs named `JUMP_CODE.<device>` through it, and fails closed when it's
down.

### 2C.1 The jump server and its jump file (Pi)

**[Pi]** This makes both key pairs and writes both files. No key is printed:

```sh
sudo sh -c '
set -e; umask 077; cd /etc/wireguard
wg genkey > wgjump.key; wg pubkey < wgjump.key > wgjump.pub
vps_key=$(wg genkey); vps_pub=$(printf "%s\n" "$vps_key" | wg pubkey)

cat > wgjump.conf <<EOF
[Interface]
Address = 10.98.0.1/30
ListenPort = 51822
MTU = 1420
PostUp = wg set %i private-key /etc/wireguard/wgjump.key
PostUp = /usr/local/sbin/tunnel-pi-exit up %i isolated
PostDown = /usr/local/sbin/tunnel-pi-exit down %i

[Peer]
# the VPS, as a jump client
PublicKey = $vps_pub
AllowedIPs = 10.98.0.2/32
EOF

cat > jump-for-vps.conf <<EOF
[Interface]
PrivateKey = $vps_key
Address = 10.98.0.2/32

[Peer]
# tunnel-pi, reached over wgpi
PublicKey = $(cat wgjump.pub)
Endpoint = 10.99.0.2:51822
EOF
'
sudo systemctl enable --now wg-quick@wgjump
```

`jump-for-vps.conf` is the jump file. Its private key belongs to the VPS and
lives on the Pi only until step 2C.2.

**Check [Pi]:** `sudo wg show wgjump` shows `listening port: 51822` and one
peer.

### 2C.2 Add the jump on the VPS

Either way works. The private key travels only through pipes and files, never
on a command line.

**Direct**, from **[Mac]**:

```sh
ssh ADMIN_HOST 'sudo cat /etc/wireguard/jump-for-vps.conf' \
  | ssh root@VPS_HOST 'wgt jump add JUMP_CODE - --label "JUMP_LABEL"'
```

**Or with wgt-ctl**, from **[Mac]**:

```sh
( umask 077; ssh ADMIN_HOST 'sudo cat /etc/wireguard/jump-for-vps.conf' \
    > ~/wg-tunnels/servers/<server>/carriers/tunnel-pi.conf )
wgt-ctl <server> jump-add          # pick tunnel-pi.conf; give it JUMP_CODE and JUMP_LABEL
```

Then delete the Pi's copy of the key. **[Pi]**:

```sh
sudo rm /etc/wireguard/jump-for-vps.conf
```

**Check [VPS]:** `wgt jump add` ends with `jump 'JUMP_CODE' works: ip=<the IP
of the Pi's network> ...`. Later, `wgt jump test` should show the same.

### 2C.3 Give a device a config through the Pi

**[VPS]**

```sh
wgt client add DEVICE --exit JUMP_CODE --warp off     # makes JUMP_CODE.DEVICE
```

`--warp off` leaves out `warp.JUMP_CODE.DEVICE`. That config would put WARP
inside the jump, so sites would see a Cloudflare IP near the friend, not the
friend's IP. Leave the flag off if you want that config too.

Install `JUMP_CODE.DEVICE` on the device (QR code or `wgt client share`).

**Check:**

- **[client]** on `JUMP_CODE.DEVICE`: `curl https://ifconfig.me` shows the IP
  of the Pi's network.
- **[VPS]** `wgt jump list` shows `JUMP_CODE` as up while the device is
  connected.

**What you get from wgt:** fail-closed when the jump is down, IPv6 blocked
through the jump, per-device routing, and a listing in `wgt client list`. DNS
goes to the config's `DNS` (by default `1.1.1.1`) through the jump, so lookups
also leave from the friend's IP.

> **Alongside wgt.** `wgpi` is still yours, not wgt's: wgt's jump only works
> while `wgpi` is up. Its backups archive all of `/etc/wireguard`, so
> `wgpi.conf` and its key go in, but `wgt restore` or `move` won't enable
> `wg-quick@wgpi`. Run `systemctl enable --now wg-quick@wgpi` afterwards.

Then 2D if the clients should reach the friend's LAN; otherwise go to phase 3.

---

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
  router. Use IPs, or in 2B, forward their local domain to the router in
  unbound (a `forward-zone` for `lan.` pointing at the router's address).
- **Not full membership.** A client doesn't get an address on their LAN. That
  would need a layer-2 bridge over the tunnel, which is beyond this guide.

### 2D.1 Turn it on (or off)

The mode belongs to the interface that runs `tunnel-pi-exit`: `wgpi` in 2B,
`wgjump` in 2C.

**[Pi]** In that interface's `.conf`, change `isolated` to `lan` on the
`PostUp = /usr/local/sbin/tunnel-pi-exit up %i ...` line, then restart it:

```sh
sudo systemctl restart wg-quick@wgpi      # 2B
sudo systemctl restart wg-quick@wgjump    # 2C
```

Changing `lan` back to `isolated` and restarting turns it off again.
cloudflared doesn't depend on either interface, so this is safe to do over
`ssh ADMIN_HOST` after drop-off.

**Check [Pi]:** `sudo iptables -L FORWARD -n` shows an `ACCEPT` to the LAN
subnet (for example `192.168.1.0/24`) **above** the `DROP` lines for the
private ranges.

### 2D.2 Who gets it

The Pi sees every client as one address (the VPS's NAT), so on the Pi the mode
applies to **all** Pi-exit clients. For some clients only:

- **2B:** keep the Pi in `lan` mode, and on the VPS block the clients that
  shouldn't have it. Add these lines to the VPS's `wgpi.conf`, one pair per
  client, with `FRIEND_LAN` the subnet from the check above:

  ```ini
  PostUp = iptables -I FORWARD -i wg0 -o %i -s CLIENT_IP -d FRIEND_LAN -j REJECT
  PostDown = iptables -D FORWARD -i wg0 -o %i -s CLIENT_IP -d FRIEND_LAN -j REJECT
  ```

  If the friend's DHCP ever moves their LAN to another subnet, update
  `FRIEND_LAN` here.

- **2C:** run **two** jump servers on the Pi, one in each mode, as two jumps.
  Repeat 2C.1–2C.2 with a second interface (`wgjumplan`; addresses
  `10.98.0.5/30` on the Pi, `10.98.0.6/32` in its jump file; port `51823`) in
  `lan` mode, and a second code (for example `homelan`). A device then picks
  its mode by config: `home.phone` (isolated) or `homelan.laptop` (LAN).

### 2D.3 Watch for overlapping subnets

LAN mode only works when the friend's LAN subnet (often `192.168.0.0/24` or
`192.168.1.0/24`) doesn't overlap any of these:

- **The client's own local network.** A laptop on a café network that also
  uses `192.168.1.0/24` sends those addresses to the café, not to the tunnel.
- **The VPS's client subnet or wgt's `10.9.8.0/24`.**
- **The tunnel's links**, `10.99.0.0/30` and `10.98.0.0/29`.

There's no fix on your side for the first one: the friend's subnet is what it
is. When the client's network is the same, use the client from another network
(a phone hotspot works).

Go to phase 3.

---

# Phase 3: prove and drop off

## 3.1 The full test, on a phone hotspot

Move the Pi onto a **phone hotspot**. It copies the friend's network: a NAT
you don't control and no open ports. Use a USB-to-ethernet adapter, or set up
the hotspot's Wi-Fi temporarily with `sudo nmtui` (and remove it after, so the
Pi uses only ethernet at the friend's). Run the tests from your Mac and client
on a **different** network from the hotspot.

Here, "the Pi network's public IP" is the hotspot's: check it on the phone, or
run `curl https://ifconfig.me` on the Pi. At the friend's it becomes theirs.

| # | Test | Pass |
|---|---|---|
| 1 | **[Mac]** `ssh ADMIN_HOST` | a shell |
| 2 | **[client]** (Pi-exit config) `curl https://ifconfig.me` | the Pi network's public IP |
| 3 | **[client]** a DNS leak test site (for example dnsleaktest.com) | 2B: only the Pi network's ISP. 2C: Cloudflare resolvers, reached from the Pi's IP |
| 4 | **[client]** `curl -6 -m 10 https://ifconfig.me` | fails (IPv6 blocked) |
| 5 | **[Pi]** `sudo systemctl stop wg-quick@wgpi`, then **[client]** `curl -m 10 https://ifconfig.me` | times out. It must **not** print the VPS's IP |
| 6 | with wgpi still stopped: **[Mac]** `ssh ADMIN_HOST` | still a shell. Then **[Pi]** `sudo systemctl start wg-quick@wgpi` |
| 7 | **[client]** `ping -c2` an address on the Pi's LAN (for example its router, `192.168.1.1`) | isolated: no reply. LAN mode (2D): replies |
| 8 | **[client]** a config that is **not** a Pi exit: `curl https://ifconfig.me` | unchanged: the VPS's or WARP's IP, as before |

## 3.2 Survive a reboot

Unplug the Pi's power and plug it back in, as it will be at the friend's.

**Check:** about a minute later, with no one touching the Pi, `ssh ADMIN_HOST`
works, `sudo wg show wgpi` shows a fresh handshake, and test 2 passes again.
A Pi that passes this is ready: drop-off is the same power-up on a different
network.

## 3.3 Harden

- **The SD card:** power cuts wear it out. The best fix is an SSD (USB boot),
  set up before phase 1. With an SD card, `sudo raspi-config` → Performance →
  Overlay file system protects it, but then nothing is saved across reboots,
  including the updates from `unattended-upgrades`. Each change, update
  included, means turning the overlay off remotely, rebooting, changing, and
  turning it back on.
- **Updates:** `unattended-upgrades` (from 1.1) keeps installing security
  fixes when the root file system is writable.
- After any change here, repeat 3.2 (unplug and plug back in).

## 3.4 Drop-off

The friend plugs in ethernet and power. That's all. Everything comes up on its
own.

**Check (from your side), within a few minutes:** `ssh ADMIN_HOST` works, and a
Pi-exit client's `curl https://ifconfig.me` shows the friend's IP. Running tests
2–8 of 3.1 again is a good idea.

If it doesn't come up, ask the friend to check that the ethernet link lights
are on and to unplug the power and plug it back in. If it still doesn't come
up, their network is blocking something the hotspot didn't: usually outbound
UDP to `PI_PORT`, which an occasional router or ISP filters. You can still
reach the Pi over `ssh ADMIN_HOST`: see Troubleshooting.

---

## Undo

**2B [VPS]**

```sh
systemctl disable --now wg-quick@wgpi tunnel-pi-killswitch.service
rm /etc/systemd/system/tunnel-pi-killswitch.service; systemctl daemon-reload
```

**2C [VPS]**

```sh
wgt client remove JUMP_CODE.DEVICE     # each config using the jump
wgt jump remove JUMP_CODE
systemctl disable --now wg-quick@wgpi
```

**Both [VPS]**

```sh
rm /etc/wireguard/wgpi.conf /etc/wireguard/wgpi.key /etc/wireguard/wgpi.pub
ufw delete allow PI_PORT/udp
```

**[Pi]** `sudo systemctl disable --now wg-quick@wgpi wg-quick@wgjump`. This
leaves only the base and admin from phase 1. To retire the Pi completely, also run
`sudo systemctl disable --now cloudflared`, then delete the tunnel and the
Access application in the dashboard.

**Check [VPS]:** `ip rule` shows nothing at `900`/`901`, `wgt jump list` no
longer shows `JUMP_CODE`, and the clients are back on their normal routes.

## Troubleshooting

| Symptom | Look at |
|---|---|
| `ssh ADMIN_HOST` fails | is the tunnel Healthy in the dashboard? Does the Access policy include your email? If you can reach the Pi any other way: `systemctl status cloudflared`. Otherwise the friend unplugs it and plugs it back in |
| No handshake on `wgpi` | **[VPS]** is `PI_PORT/udp` open (`ss -ulpn`, the firewall)? **[Pi]** does `VPS_HOST` resolve? Are the public keys swapped? |
| 2B: handshake, but the client has no internet | **[VPS]** `ip rule` (900 first?), `ip route show table 260`, the `FORWARD` counters. **[Pi]** `sysctl net.ipv4.ip_forward`, `sudo iptables -t nat -L -v` |
| 2B: sites load but names don't resolve | **[Pi]** `systemctl status unbound`, `dig @10.99.0.2 example.com`. **[VPS]** `iptables -t nat -L PREROUTING -v` |
| 2C: `jump add` fails its check | **[VPS]** `ping 10.99.0.2` (the link first), then `wg show px-JUMP_CODE`. **[Pi]** `sudo wg show wgjump` (a handshake from 10.99.0.1?), `sysctl net.ipv4.ip_forward` |
| Some sites hang (usually large pages) | MTU. wgt's client configs use 1280, which fits through both tunnels. A config with a larger `MTU` needs lowering |
| A client shows the VPS's IP | 2B: `systemctl status tunnel-pi-killswitch`, `ip rule` (901). 2C: is the config named `JUMP_CODE.<device>`? |

## Limits and care

- **Speed:** your downloads are the friend's uploads, so their upload speed
  sets your ceiling. Every request also takes the extra trip
  client → VPS → Pi.
- **Their IP changes:** this is fine. The Pi dials out again and the VPS
  learns the new address.
- **Your VPS's IP changes:** `wg-quick` looks up `Endpoint` only when it
  starts, so restart wgpi on the Pi over `ssh ADMIN_HOST`.
- **Other things that reveal you:** the IP, DNS and IPv6 are covered. WebRTC,
  the browser's time zone and locale, phone location services and your account
  history aren't. Strict services (banks, streaming) look at these.
- **The friend:** to their ISP, all of this traffic is their household's. Let
  only clients you trust use this exit, and tell the friend what the box does.
