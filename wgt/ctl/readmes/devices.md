# devices/ (wireguard-tools)

One folder per device (a phone, a laptop, a router). For each client config a
[wireguard-tools](https://github.com/cgmckeever/wireguard-tools) server issues,
`wgt-ctl` keeps a copy in `<device>/config/<server>.<config>.conf`. The copy on
the server is the live one: `wgt-ctl <server> sync` brings these back in line,
and replaced copies are kept as `.bak` files. They hold private keys.

The servers themselves are in `../fleet/` (see `README-tools.md` there).

wireguard-tools writes this file when it's missing, and never changes it.
