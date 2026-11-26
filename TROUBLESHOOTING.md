# wireguard-tools: Troubleshooting

For the [wireguard-tools README](README.md).

- **Devices can't connect:** the provider firewall usually still blocks UDP
  51820 (see [Quick start](README.md#quick-start)).
- **`wgcf register` fails:** WARP may refuse registrations from some server IPs.
  Retry later, or register on another machine and copy the account over
  ([below](#register-warp-on-another-machine)). If the WARP tests still fail
  (no handshake), the provider is blocking WARP's traffic: use plain configs.
- **Some sites hang through WARP:** MTU is already 1280 (the lowest allowed)
  and TCP MSS is clamped; try the device's plain config to see if it's WARP's path.
- **No internet through a jump:** `wgt jump test` says which layer
  fails. No jump handshake usually means the server was retired or the key
  revoked (Proton does both): get a new jump file and add it again (same code).
  `wgt log` shows the watchdog bringing jumps up and down.
- **A static address doesn't answer:** the device must be connected to the same
  WireGuard Server as you, with a handshake in the last 180s (`wgt client list`),
  and share a [group](README.md#groups-who-can-reach-what) with you (to reach the server,
  be in `server`). Check its own firewall and sshd, and any narrower `AllowedIPs`.
- **Handshake works but nothing loads:** the device's config is out of date
  (e.g. after a `--subnet` change) or its `Address` is mistyped; re-send it.
- **`default route changed`** (`wgt summary` says CHANGED, `wgt test` fails): setup
  stops and stops `warp`, because SSH depends on that route, so `warp.` configs stay
  blocked. Compare `ip -4 route show default` with `DEFAULT_ROUTE=` in
  `/etc/wireguard/wg-tools.state`. If the new route is right, such as after a provider
  gateway change, put the new line there and re-run `wgt setup`. `wgt rollback`
  doesn't clear it.
- **`another wireguard-tools command is running`:** a command waits 15s for another
  to finish, then gives up. Try again when it's done.
- **Anything else:** `wgt status` (marks saved settings still
  pending, i.e. not running yet) and `wgt test`.

## Register WARP on another machine

If setup reports `wgcf register failed` (it then finishes; `warp.` configs stay
blocked), register the WARP account somewhere WARP accepts (your Mac or a Linux
machine; on a Mac: `brew install wgcf`) and copy it over. Registration happens
once, and is skipped when the account file exists. The failed run already
created `/etc/wireguard/wgcf/` for it.

```bash
# on your machine
mkdir -p ~/wgcf-vps && cd ~/wgcf-vps
wgcf register --accept-tos     # a new WARP account, separate from your Mac's WARP app
wgcf generate                  # uses the API too, so do it here as well
scp wgcf-account.toml wgcf-profile.conf root@<server>:/etc/wireguard/wgcf/
ssh root@<server> 'chmod 600 /etc/wireguard/wgcf/*'
cd ~ && rm -rf ~/wgcf-vps      # local copies hold the account's private key
```

Then re-run `wgt setup` on the server; it uses the copied account and
tests WARP. Use these files on that server only: they are one WARP account.

A jump's account works the same way, and `wgt jump add` stops if it's refused.
Register a separate account per jump and copy it to `/etc/wireguard/exits/<code>/`
(`mkdir -p -m 700`) before `wgt jump add`.
