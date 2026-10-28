# wireguard-tools

Turn a bare Debian 11+ / Ubuntu 20.04+ server (amd64 or arm64) into a WireGuard
VPN server, and manage its clients. Client traffic exits one of two ways:

| `--exit` | Clients' traffic leaves via | Sites see | Use it for |
|----------|----------------------------|-----------|------------|
| `warp` (default) | [Cloudflare WARP](https://1.1.1.1/), tunnelled from the server | a Cloudflare IP | hiding the VPS's IP, getting Cloudflare's network |
| `direct` | the server's own public interface | the VPS's IP | a plain, fast WireGuard VPN |

Two commands, both run as root on the server:

- **`wg-setup.sh`** sets up the server: WARP (in `warp` mode), the `wg0`
  interface, routing/NAT, firewall, systemd. Also `test`, `status`, `backup`, `rollback`.
- **`wg-client.sh`** manages clients: `add`, `rotate`, `remove`, `list`, `show`, `share`.

Both are safe to re-run, support `--dry-run`, back up every file they change,
and never print private keys unless you ask for a QR code.

## Install

Nothing else is needed on the server; `wg-setup.sh` installs its own packages.

```bash
sudo apt-get update && sudo apt-get install -y git
sudo git clone https://github.com/cgmckeever/wireguard-tools.git /opt/wireguard-tools
cd /opt/wireguard-tools
sudo -i   # everything below runs as root
```

Optional, to run them from anywhere:
`ln -s /opt/wireguard-tools/wg-setup.sh /opt/wireguard-tools/wg-client.sh /usr/local/sbin/`

Update later with `git -C /opt/wireguard-tools pull`.

## Set up the server

Try any step with `--dry-run` first to see exactly what it will change.

### WARP exit (default)

```bash
./wg-setup.sh warp      # install wgcf, register a free WARP account, bring up `warp`, test it
                        # check its tests pass before going on
./wg-setup.sh server    # create wg0, route its clients out through warp
```

### Direct exit

```bash
./wg-setup.sh --exit direct server
```

Then **open UDP 51820** (or your `--port`) in your VPS provider's firewall /
security group. The script opens it on the server itself (ufw or iptables) but
can't reach the provider's firewall.

The mode is saved. To switch: `./wg-setup.sh rollback`, then set up again with
the other `--exit`. Existing clients keep working after a switch.

## Manage clients

```bash
./wg-client.sh add laptop                            # full tunnel (default)
./wg-client.sh add printer --allowed-ips 10.8.0.0/24 # split tunnel: VPN network only
./wg-client.sh list                                  # address, last seen, traffic, allowed IPs
./wg-client.sh show laptop                           # config with keys redacted
./wg-client.sh rotate laptop                         # new keys, same address; old config stops working
./wg-client.sh remove laptop
```

Leave out the name on `rotate`, `remove`, `show` or `share` to pick from a
numbered list. Changes apply live; other clients stay connected.

Each client gets its own keypair, a preshared key and the next free address.
Its config is written to `/root/clients/<name>.conf`.

### Getting a config onto a device

| How | Command | Best for |
|-----|---------|----------|
| Magic Wormhole | `./wg-client.sh share laptop`; recipient runs `wormhole receive <code>` | sending it to someone else, no SSH needed |
| QR code | `./wg-client.sh show laptop --qr` | phones (WireGuard app → scan) |
| scp | `scp root@<server>:/root/clients/laptop.conf .` | your own machines |

`share` prints a one-time code (like `7-guitar-revenge-tidy`) and waits up to 10
minutes. The file is encrypted end to end; a wrong guess at the code ends the
transfer. The recipient needs magic-wormhole (`brew install magic-wormhole`,
`apt install magic-wormhole`, `pipx install magic-wormhole`) and then imports
the `.conf` into their WireGuard app.

Both the QR code and the wormhole transfer carry the client's private key:
send them only to that client's owner.

## Check, back up, undo

```bash
./wg-setup.sh test              # PASS/FAIL checks; non-zero exit on any FAIL
./wg-setup.sh status            # interfaces, handshakes, routes, rules
./wg-setup.sh backup            # /root/wg-backups/wireguard-tools.<host>.<date>.tar.gz
./wg-setup.sh rollback          # stop everything, remove all rules, restore original files
./wg-setup.sh rollback --purge  # ...and delete keys, clients and the WARP account
```

`test` checks, depending on the mode: WARP is up and handshaking, traffic
through `warp` shows `warp=on` while the server's own traffic shows `warp=off`,
the server's default route never changed, `wg0` is listening, forwarding is
on, every routing/NAT rule exists exactly once, the services start at boot in
the right order, and whether any client has connected.

A backup holds private keys (it's created mode 600). To restore on a fresh
server: `tar -xzpf <archive> -P`, then run `./wg-setup.sh server` (after
`warp`, in `warp` mode).

## Options

Flags work on both commands. Settings chosen at setup are saved in
`/etc/wireguard/wg-tools.state`, so later runs and `wg-client.sh` use them
automatically. Precedence: flag > environment variable > saved > default.

| Flag | Env | Default | |
|------|-----|---------|---|
| `--exit` | `WG_EXIT` | `warp` | `warp` or `direct` |
| `--subnet` | `WG_SUBNET` | `10.8.0.0/24` | VPN network; server is `.1` |
| `--port` | `WG_PORT` | `51820` | UDP listen port |
| `--mtu` | `WG_MTU` | `1280` warp, `1420` direct | tunnel MTU for server and clients |
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
`wg-quick@wg0` starts after, and stops with, `wg-quick@warp`.

**Direct mode.** Clients are NATed out the public interface; no extra routing.

**Both.** All routing/NAT/firewall rules live in `wg0.conf`'s
`PostUp`/`PostDown`, generated from one list so `PostDown` and `rollback` undo
exactly what `PostUp` did. Forward rules are inserted at the top so ufw/Docker
can't block them, and TCP MSS is clamped to avoid MTU stalls. Clients'
`AllowedIPs` include `::/0` so their IPv6 can't bypass the tunnel (clients get
IPv4 only, so apps fall back to it).

### Files

| Path | What |
|------|------|
| `/etc/wireguard/wg0.conf` | server interface + rules; `[Peer]`s rebuilt from `clients.db` |
| `/etc/wireguard/warp.conf` | WARP tunnel (warp mode) |
| `/etc/wireguard/wgcf/` | WARP account and profile |
| `/etc/wireguard/server.key`, `server.pub` | server keypair |
| `/etc/wireguard/clients.db` | `name address public-key allowed-ips`, no private keys |
| `/etc/wireguard/psk/<name>.psk` | per-client preshared keys |
| `/root/clients/<name>.conf` | client configs (the only copy of each client's private key) |
| `/etc/wireguard/wg-tools.state` | saved settings, recorded default route |
| `/etc/sysctl.d/99-wg.conf` | `net.ipv4.ip_forward = 1` |
| `/etc/systemd/system/wg-quick@wg0.service.d/` | start `wg0` after `warp` (warp mode) |

Secrets are mode 600 in 700 directories. Every change leaves a timestamped
`.bak`; `rollback` restores the originals.

## Troubleshooting

- **Clients can't connect:** the provider firewall usually still blocks UDP 51820.
- **`wgcf register` fails:** WARP may refuse registrations from some server IPs.
  Retry later, or register on another machine and copy the account over
  ([below](#register-warp-on-another-machine)). If the WARP tests still fail
  (no handshake), the provider is blocking WARP's traffic: use `--exit direct`.
- **Some sites hang in WARP mode:** lower `--mtu` (e.g. 1200), re-run `server`, then `rotate` the clients so they get the new MTU.
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
carries on. Use these files on that server only: they are one WARP device.

## Upgrading from the old scripts

The earlier `scripts/` versions (`setup.sh`, `client-setup.sh`, …) are in git
history. The new tools won't overwrite a `wg0.conf` they didn't create
(`--force` overrides). To move a server over: back up `/etc/wireguard`, run
`wg-setup.sh` (with `--force`), then re-add clients with `wg-client.sh add`.
