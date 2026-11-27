"""~/wg-tunnels ($WG_TUNNELS): everything this machine keeps about its servers,
their devices and the pucks, shared with wireguard-tunnels. Nothing private is
kept in the checkout. Folders are 0700, files 0600.

  devices/<device>/config/           client configs as the servers issued them (tunnels.py)
  fleet/servers/<server>/login       how wgt-ctl reaches it: "user@host [/checkout/dir]"
  fleet/servers/<server>/backups/    archives fetched by backup and move (private keys)
  fleet/servers/<server>/carriers/   jump files staged for it (tunnels.py, jumpfiles.py)
  fleet/device-list.json             each device's static number, fleet-wide (fleet.py)
  fleet/pucks/<puck>/                tunnel-puck's settings for one puck (tpk-ctl.sh)

A server is listed when its folder has a login file; wireguard-tunnels' nord.sh
offers the same servers.
"""

from __future__ import annotations

import os

from ..errors import WgtError


def root() -> str:
    """$WG_TUNNELS as written (as wireguard-tunnels reads it), else ~/wg-tunnels. An
    absolute path only: a relative one (or a "~" the shell didn't expand) would put
    configs, private keys and all, under wherever wgt-ctl runs, a checkout say."""
    path = os.environ.get("WG_TUNNELS") or os.path.expanduser("~/wg-tunnels")
    if not os.path.isabs(path):
        raise WgtError(f"WG_TUNNELS must be an absolute path, not '{path}'")
    return path


def devices_dir() -> str:
    return os.path.join(root(), "devices")


def config_dir(device: str) -> str:
    return os.path.join(devices_dir(), device, "config")


def servers_dir() -> str:
    return os.path.join(root(), "fleet", "servers")


def server_dir(server: str) -> str:
    return os.path.join(servers_dir(), server)


def login_file(server: str) -> str:
    return os.path.join(server_dir(server), "login")


def backups_dir(server: str) -> str:
    return os.path.join(server_dir(server), "backups")


def carriers_dir(server: str) -> str:
    """Where <server>'s jump files wait to be pushed."""
    return os.path.join(server_dir(server), "carriers")


def devices_file() -> str:
    return os.path.join(root(), "fleet", "device-list.json")


def pucks_dir() -> str:
    """tunnel-puck's: tpk-ctl.sh keeps each puck's settings in <this>/<puck>/."""
    return os.path.join(root(), "fleet", "pucks")


def make_dir(path: str) -> str:
    """<path> and any missing folders up from it to the root, each 0700."""
    top = root()
    todo = []
    d = path
    while not os.path.isdir(d):
        todo.append(d)
        if d == top or os.path.dirname(d) == d:
            break
        d = os.path.dirname(d)
    for d in reversed(todo):
        os.makedirs(d, exist_ok=True)
        os.chmod(d, 0o700)
    return path


def shown(path: str) -> str:
    """A path as people read it: under ~ when it is."""
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path
