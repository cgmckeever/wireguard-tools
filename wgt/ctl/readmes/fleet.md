# fleet/ (wireguard-tools)

The systems [wireguard-tools](https://github.com/cgmckeever/wireguard-tools)
manages from this machine. `wgt-ctl` and tunnel-puck's `tpk-ctl.sh` keep it
up to date: change it through them rather than by hand.

- `servers/<server>/`: one server each. `login` is how `wgt-ctl` reaches it,
  `backups/` holds archives fetched from it (they hold private keys), and
  `carriers/` holds jump files waiting to be pushed to it.
- `pucks/<puck>/`: tunnel-puck's settings for one puck, a small computer on a
  friend's home network that a server's clients can exit through.
- `device-list.json`: each device's static number, the same on every server.

The client configs the servers issue go to `../devices/<device>/config/`
(see `README-tools.md` there).

wireguard-tools writes this file when it's missing, and never changes it.
