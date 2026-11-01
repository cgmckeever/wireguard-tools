"""Fixed paths and constants. Paths under the WireGuard directory are built
from a root (default /etc/wireguard), so tests can point everything at a
fixture directory."""

from __future__ import annotations

import os
from dataclasses import dataclass

# Settings saved in the state file by setup (and warp on|off), in this order.
CONFIG_KEYS = (
    "WARP", "SERVER_NAME", "WG_SUBNET", "WG_SUBNET6", "WG_PORT", "WG_MTU", "WARP_MTU",
    "WG_DNS", "WG_IF", "WARP_IF", "RT_TABLE", "CLIENT_ALLOWED_IPS",
)

DEFAULTS = {
    "WARP": "on", "WG_PORT": "51820", "WARP_MTU": "1280", "WG_MTU": "1280",
    "WG_DNS": "1.1.1.1", "WG_IF": "wg0", "WARP_IF": "warp", "RT_TABLE": "100",
    "CLIENT_ALLOWED_IPS": "0.0.0.0/0, ::/0",
}

RULE_PRIO = 10000
BLACKHOLE_METRIC = 4096
MARKER = "# Managed by wireguard-tools"
TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"

EXIT_TABLE_BASE = 200        # client table of exit idx: 200+idx
EXIT_PTABLE_BASE = 230       # Proton table of exit idx: 230+idx
EXIT_MARK_BASE = 22272       # fwmark of exit idx: 0x5700+idx
EXIT_MAX = 20
EXIT_RULE_PRIO = 9500        # per-client rules: before the WARP rule (RULE_PRIO)
EXIT_MARK_PRIO = 9400
EXIT_PROTON_MTU = 1420
EXIT_IDLE = 300              # seconds without a client handshake before an exit goes down

LOG_TAG = "wg-tools"            # journal tag for the changes wgt makes
WATCHDOG = "wg-tools-warp-watchdog"
EXIT_WATCHDOG = "wg-tools-exit-watchdog"
EXIT_HOLD = 60               # seconds a hold file (exit test) keeps the watchdog off a city
WATCHDOG_STALE = 180         # seconds without a WARP handshake before it acts

KEEP_CHANGES = 5             # newest copies kept per file (plus its original)
KEEP_ARCHIVES = 10           # newest `backup` archives kept

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


@dataclass(frozen=True)
class Paths:
    """Where everything lives. `wg_dir` and the system paths can be moved for tests."""

    wg_dir: str = "/etc/wireguard"
    backup_dir: str = "/var/backups/wireguard-tools"   # mode 700; outside the checkout
    sysctl_file: str = "/etc/sysctl.d/99-wg.conf"
    systemd_dir: str = "/etc/systemd/system"
    sbin_dir: str = "/usr/local/sbin"
    wgcf_bin: str = "/usr/local/bin/wgcf"
    lock_file: str = "/run/wg-tools.lock"
    run_dir: str = "/run"
    hosts_file: str = "/etc/hosts"
    cloud_cfg_dir: str = "/etc/cloud/cloud.cfg.d"

    def wg(self, *parts: str) -> str:
        return os.path.join(self.wg_dir, *parts)

    @property
    def state(self) -> str:
        return self.wg("wg-tools.state")

    @property
    def backup_map(self) -> str:
        return self.wg("wg-tools.backups")

    @property
    def client_db(self) -> str:
        return self.wg("clients.db")

    @property
    def disabled_db(self) -> str:
        return self.wg("clients.disabled")

    @property
    def psk_dir(self) -> str:
        return self.wg("psk")

    @property
    def client_dir(self) -> str:
        return self.wg("clients")

    @property
    def server_key(self) -> str:
        return self.wg("server.key")

    @property
    def server_pub(self) -> str:
        return self.wg("server.pub")

    @property
    def wgcf_dir(self) -> str:
        return self.wg("wgcf")

    @property
    def exit_dir(self) -> str:
        return self.wg("exits")

    @property
    def exit_db(self) -> str:
        return self.wg("exits.db")

    def wgcf_profile(self, directory: str = "") -> str:
        """The WARP profile wgcf generated, in <directory> (default the main one)."""
        return os.path.join(directory or self.wgcf_dir, "wgcf-profile.conf")

    def exit_wgcf_dir(self, code: str) -> str:
        """Exit city <code>'s own WARP account."""
        return self.wg("exits", code)

    def proton_conf(self, code: str) -> str:
        """Exit city <code>'s Proton config as given (with its label comment)."""
        return self.wg("exits", f"{code}.proton.conf")

    def px_conf(self, code: str) -> str:
        return self.wg(f"px-{code}.conf")

    def wx_conf(self, code: str) -> str:
        return self.wg(f"wx-{code}.conf")

    def exit_marker(self, code: str) -> str:
        """The exit watchdog's record of when it brought <code> up."""
        return os.path.join(self.run_dir, f"{EXIT_WATCHDOG}.{code}")

    @property
    def warp_marker(self) -> str:
        """The WARP watchdog's note of an unhealthy run (it acts on the second)."""
        return os.path.join(self.run_dir, WATCHDOG)

    def exit_hold(self, code: str) -> str:
        """While it exists, the exit watchdog leaves <code> up (exit test)."""
        return os.path.join(self.run_dir, f"{EXIT_WATCHDOG}.{code}.hold")

    @property
    def change_dir(self) -> str:
        return os.path.join(self.backup_dir, "files")

    def unit_file(self, name: str) -> str:
        return os.path.join(self.systemd_dir, name)


# The settings table (setup's review, status): key, label, default. A row with
# a default is only shown when its value differs from it.
SETTING_ROWS = (
    ("SERVER_NAME", "name", ""), ("ENDPOINT", "endpoint", ""), ("WG_PORT", "port", ""),
    ("WG_SUBNET", "subnet", ""), ("WG_SUBNET6", "subnet6", ""), ("WARP", "WARP", ""),
    ("WG_DNS", "DNS", ""), ("CLIENT_ALLOWED_IPS", "allowed IPs", ""), ("WG_MTU", "MTU", ""),
    ("WARP_MTU", "WARP MTU", "1280"), ("WG_IF", "interface", "wg0"), ("WARP_IF", "WARP interface", "warp"),
    ("RT_TABLE", "table", "100"),
)
