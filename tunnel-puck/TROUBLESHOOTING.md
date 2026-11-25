# tunnel-puck: Troubleshooting

For the steps in the [tunnel-puck README](README.md).

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
| `tpk`: handshake, but no internet on the client | **[Gateway]** `ip rule` (this puck's rules, [Fixed values](README.md#fixed-values)), `ip route show table <the puck's table>` ([Fixed values](README.md#fixed-values)); **[Puck]** `sudo iptables -t nat -L -v`, `journalctl -u wg-quick@wgpk<n>` (its `tunnel-puck-exit:` line names the uplink) |
| `tpk`: sites load, names don't resolve | **[Puck]** `systemctl status unbound`; **[Gateway]** `iptables -t nat -L PREROUTING -v` |
| large pages hang | MTU: a client config's `MTU` above 1280 needs lowering. The jump runs inside the link, so a packet much bigger than a client's 1280 gets split in two on the way, and behind a busy NAT that can cut the speed to a few Mbit/s |
| a client shows the gateway's IP | `tpk`: is it in `CLIENT_IPS`? `systemctl status tunnel-puck-killswitch-<puck>`. `wgt`: is the config named `<code>.<device>`? |
| FAIL `the LAN-only jump ... sends ... into the puck, but its LAN is ...` | the puck is on another network since `exit`: `scripts/tpk-ctl.sh <puck>.<gateway> exit` sets the jump's routes to its LAN |
| FAIL `the puck reports no LAN for the LAN-only jump` | the puck's `tunnel-puck-exit` predates it (`scripts/tpk-ctl.sh <puck>.<gateway> exit` installs the current one), or its uplink has no IPv4 address (`test`'s `uplink:` line) |
| FAIL `wgt jump routes <code> gave nothing` | the LAN-only jump isn't on the gateway (`exit` adds it), or wgt there is older than routes-only jumps: `../bin/wgt-ctl <gateway> pull` |
| traffic stops after the puck changes networks | **[Puck]** `journalctl -t tunnel-puck-exit -n 20` shows each move; if none, reboot the puck |
| with the LAN: some LAN addresses don't answer | the friend's LAN overlaps the client's own network (a café on `192.168.1.0/24` too), a subnet of `WG_IF`, or the link's addresses ([Fixed values](README.md#fixed-values)). Try the client from another network |
