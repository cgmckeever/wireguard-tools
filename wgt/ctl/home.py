"""~/wg-tunnels ($WG_TUNNELS): everything this machine keeps about the servers,
their devices and the pucks, shared with wireguard-tunnels. Nothing private is
kept in the checkout.

  servers/<server>/login       how wgt-ctl reaches it: "user@host [/checkout/dir]"
  servers/<server>/backups/    archives fetched by wgt-ctl backup and move (private keys)
  servers/<server>/carriers/   jump files staged for it (tunnels.py, jumpfiles.py)
  fleet/devices                each device's static number, fleet-wide (fleet.py)
  devices/<device>/config/     client configs as the servers issued them (tunnels.py)
  pucks/<puck>/                tunnel-puck's settings for one puck (tpk-ctl.sh)
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


def servers_dir() -> str:
    return os.path.join(root(), "servers")


def server_dir(server: str) -> str:
    return os.path.join(servers_dir(), server)


def login_file(server: str) -> str:
    """Its line in the server list: a server is listed when it has one."""
    return os.path.join(server_dir(server), "login")


def backups_dir(server: str) -> str:
    return os.path.join(server_dir(server), "backups")


def devices_file() -> str:
    return os.path.join(root(), "fleet", "devices")


def shown(path: str) -> str:
    """A path as people read it: under ~ when it is."""
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def list_shown() -> str:
    """Where the server list is, for messages."""
    return shown(os.path.join(servers_dir(), "<server>", "login"))
