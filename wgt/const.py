"""Fixed paths and constants. Paths under the WireGuard directory are built
from a root (default /etc/wireguard), so tests can point everything at a
fixture directory."""

from __future__ import annotations

import os
from dataclasses import dataclass

# Settings saved in the state file by setup, in this order (ENDPOINT last: SETTING_KEYS).
CONFIG_KEYS = (
    "SERVER_NAME", "WG_SUBNET", "WG_SUBNET6", "WG_PORT", "WG_MTU", "WARP_MTU",
    "WG_DNS", "WG_IF", "WARP_IF", "RT_TABLE", "CLIENT_ALLOWED_IPS",
)

# All of them (setup saves them together): ENDPOINT has no default (setup detects it,
# and a re-run keeps it).
SETTING_KEYS = CONFIG_KEYS + ("ENDPOINT",)
# The ones written into configs on devices (setup rewrites those: sync_client_configs),
# so a change to one means re-sending them.
CLIENT_CONFIG_KEYS = ("SERVER_NAME", "ENDPOINT", "WG_PORT", "WG_SUBNET", "WG_SUBNET6", "WG_DNS", "WG_MTU")

# In the state file once setup has finished (rollback clears it). WARP is how
# older versions said so; it goes at a server's next setup.
SETUP_DONE = "SETUP_DONE"
LEGACY_SETUP_DONE = "WARP"
SET_UP_KEYS = (SETUP_DONE, LEGACY_SETUP_DONE)

DEFAULTS = {
    "WG_PORT": "51820", "WARP_MTU": "1280", "WG_MTU": "1280",
    "WG_DNS": "1.1.1.1", "WG_IF": "wg0", "WARP_IF": "warp", "RT_TABLE": "100",
    "CLIENT_ALLOWED_IPS": "0.0.0.0/0, ::/0",
}

WARP_RULE_PRIO = 10000
BLACKHOLE_METRIC = 4096
MARKER = "# Managed by wireguard-tools"
TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"

EXIT_TABLE_BASE = 200        # client table of exit idx: 200+idx
EXIT_PTABLE_BASE = 230       # jump table of exit idx: 230+idx
EXIT_MARK_BASE = 22272       # fwmark of exit idx: 0x5700+idx
EXIT_MAX = 20
EXIT_RULE_PRIO = 9500        # per-client rules: before the WARP rule (WARP_RULE_PRIO)
EXIT_MARK_PRIO = 9400
DIRECT_STOP_PRIO = 9501      # WARP-off host clients: blackhole after their main-table rule
JUMP_MTU = 1420
EXIT_IDLE = 300              # seconds without a client handshake before an exit goes down

# Static device addresses: one number per device, the same on every server.
DEVICE_NET = "10.9.8.0/24"
DEVICE_ADDRESS_FORM = DEVICE_NET.rsplit(".", 1)[0] + ".<number>"   # for people: 10.9.8.<number>
DEVICE_SERVER = 1            # the server itself
DEVICE_CTL_FIRST, DEVICE_CTL_LAST = 2, 127          # handed out by wgt-ctl, counting up
DEVICE_DIRECT_FIRST, DEVICE_DIRECT_LAST = 128, 254  # handed out by `wgt client add`, counting down
DEVICE_ACTIVE = 180          # seconds since a handshake for a config to count as in use
DEVICE_CHAIN = "WGT-DEVICES"       # nat: a static address -> the config in use
DEVICE_SOURCE_CHAIN = "WGT-DEVICE-SRC"  # nat: a sender's config address -> its static address
DEVICE_PEER_CHAIN = "WGT-PEERS"    # filter: which devices may reach each other (none listed: refused)
DEVICE_SERVER_CHAIN = "WGT-SERVER" # filter: which devices may reach the server's tunnel addresses
SERVER_GROUP = "server"      # the group that reaches the server, and not the others in it
DEVICE_RULE_PRIO = 9300      # before every client rule: no config in use means unreachable, not a way out

LOG_TAG = "wg-tools"            # journal tag for the changes wgt makes
WARP_WATCHDOG = "wg-tools-warp-watchdog"
EXIT_WATCHDOG = "wg-tools-exit-watchdog"
DEVICE_WATCHDOG = "wg-tools-device-watchdog"
RULES_RESTART_GAP = 300      # seconds between the device watchdog's restarts of wg0 for missing rules
EXIT_HOLD = 60               # seconds a hold file (jump test) keeps the watchdog off a jump
# Seconds a jump check gives a just-started jump for its WARP handshake (it returns as
# soon as there is one; usually 5-10s). Under EXIT_HOLD, so the watchdog stays off
# meanwhile. A WARP that doesn't answer at all (seen on a Nord jump started over and
# over) isn't helped by waiting longer.
EXIT_WARP_WAIT = 25
WATCHDOG_STALE = 180         # seconds without a handshake before a watchdog acts on a tunnel


@dataclass(frozen=True)
class Watchdog:
    """One watchdog: its systemd units, and `wgt watchdog <word>` (watchdog.runs) that they run."""

    name: str              # its units' name and its journal tag
    word: str
    label: str             # "<label> watchdog" in the timer's description and `wgt test`
    service_what: str      # the service's description ({warp_if}: the WARP interface)
    step_what: str         # setup's step when it starts the timer (the same)
    timeout: int           # seconds a stuck run may take
    boot: int              # seconds after boot of the first run
    every: int             # seconds between runs
    accuracy: int
    jumps_only: bool = False   # only runs on a server with jumps

    @property
    def check_text(self) -> str:
        return f"{self.label} watchdog timer enabled and running"


# A new watchdog is a row here (rollback, `wgt log`, setup, status and `wgt test` read
# this) and its function in watchdog.runs.
WATCHDOGS = (
    Watchdog(WARP_WATCHDOG, "warp", "WARP",
             "WARP watchdog (new source port if {warp_if} stops handshaking)",
             f"WARP watchdog ({WARP_WATCHDOG}.timer: new source port for {{warp_if}} after "
             f"{WATCHDOG_STALE}s without a handshake)",
             timeout=60, boot=30, every=30, accuracy=5),
    Watchdog(EXIT_WATCHDOG, "exits", "jump",
             "jump watchdog (jumps up while their clients are active)",
             f"jump watchdog ({EXIT_WATCHDOG}.timer: jumps up while their clients are active, "
             f"down after {EXIT_IDLE}s idle)",
             timeout=30, boot=20, every=10, accuracy=2, jumps_only=True),
    Watchdog(DEVICE_WATCHDOG, "devices", "device",
             "device watchdog (static addresses follow the config in use)",
             f"device watchdog ({DEVICE_WATCHDOG}.timer: static addresses follow the config in use)",
             timeout=30, boot=20, every=10, accuracy=2),
)
ALL_WATCHDOGS = tuple(w.name for w in WATCHDOGS)


def error_tag(tag: str) -> str:
    """The journal tag a watchdog's failed runs log under: its own tag is what it did
    (the last WARP port change is the WARP watchdog's last entry)."""
    return f"{tag}-error"

MIB = 1024 * 1024
KEEP_CHANGES = 5             # newest copies kept per file (plus its original)
# What setup records about this host (wg-tools.state): restore drops them
# (they belong to the old server) and rollback uses them. LOCATION too: setup
# looks up the new machine's city (restore --location sets one).
ORIG_HOSTNAME, SET_HOSTNAME = "ORIG_HOSTNAME", "SET_HOSTNAME"   # setup's rename, for rollback
HOST_STATE_KEYS = ("DEFAULT_ROUTE", "PUB_IF", "ORIG_IP_FORWARD", "ORIG_IP6_FORWARD", "ORIG_ACCEPT_RA", "UFW",
                   "UFW_PORTS", ORIG_HOSTNAME, SET_HOSTNAME, "LOCATION")
KEEP_ARCHIVES = 10           # newest `backup` archives kept

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


def archive_name(host: str, stamp: str) -> str:
    """A `wgt backup` archive's file name (a "*" for either part makes a glob)."""
    return f"wireguard-tools.{host}.{stamp}.tar.gz"


def client_conf_name(server: str, name: str) -> str:
    """A client config's file name, <server>.<name>.conf: on the server, and its copy in
    ~/wg-tunnels (wgt-ctl), which keeps the server's name for it."""
    return f"{server}.{name}.conf"


def jump_source_name(code: str) -> str:
    """The file name of jump <code>'s config as given (any provider's: the name stays for running servers)."""
    return f"{code}.proton.conf"


@dataclass(frozen=True)
class Paths:
    """Where everything lives. `wg_dir` and the system paths can be moved for tests."""

    wg_dir: str = "/etc/wireguard"
    backup_dir: str = "/var/backups/wireguard-tools"   # mode 700; outside the checkout
    sysctl_file: str = "/etc/sysctl.d/99-wg.conf"
    networkd_file: str = "/etc/systemd/networkd.conf.d/99-wg-tools.conf"
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
    def disabled_all_db(self) -> str:
        """The configs `client disable --all` turned off, for `enable --all`."""
        return self.wg("clients.disabled-all")

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
    def device_db(self) -> str:
        return self.wg("devices.db")

    @property
    def exit_dir(self) -> str:
        return self.wg("exits")

    @property
    def exit_db(self) -> str:
        return self.wg("exits.db")

    def psk(self, name: str) -> str:
        """Client <name>'s preshared key."""
        return os.path.join(self.psk_dir, f"{name}.psk")

    def client_conf(self, server: str, name: str) -> str:
        """A client's config as handed out: <server>.<name>.conf."""
        return os.path.join(self.client_dir, client_conf_name(server, name))

    @property
    def wgt_link(self) -> str:
        """The link setup makes so `wgt` runs from anywhere."""
        return os.path.join(self.sbin_dir, "wgt")

    def wgcf_account(self, directory: str = "") -> str:
        """The WARP account wgcf registered, in <directory> (default the main one)."""
        return os.path.join(directory or self.wgcf_dir, "wgcf-account.toml")

    def wgcf_profile(self, directory: str = "") -> str:
        """The WARP profile wgcf generated, in <directory> (default the main one)."""
        return os.path.join(directory or self.wgcf_dir, "wgcf-profile.conf")

    def wgcf_scratch(self, directory: str) -> str:
        """Where `warp rotate` registers <directory>'s new account first. Removed after,
        but a run stopped there leaves it, a registered account's key inside: whatever
        removes <directory> removes this too."""
        return directory.rstrip("/") + ".new"

    def exit_wgcf_dir(self, code: str) -> str:
        """Jump <code>'s own WARP account."""
        return os.path.join(self.exit_dir, code)

    def jump_source(self, code: str) -> str:
        """Jump <code>'s jump config as given (with its label comment)."""
        return os.path.join(self.exit_dir, jump_source_name(code))

    def px_conf(self, code: str) -> str:
        return self.wg(f"px-{code}.conf")

    def wx_conf(self, code: str) -> str:
        return self.wg(f"wx-{code}.conf")

    def exit_marker(self, code: str) -> str:
        """The exit watchdog's record of when it brought <code> up."""
        return os.path.join(self.run_dir, f"{EXIT_WATCHDOG}.{code}")

    @property
    def rules_marker(self) -> str:
        """When the device watchdog last restarted wg0 for its missing rules."""
        return os.path.join(self.run_dir, f"{DEVICE_WATCHDOG}.rules")

    @property
    def warp_marker(self) -> str:
        """The WARP watchdog's note of an unhealthy run (it acts on the second)."""
        return os.path.join(self.run_dir, WARP_WATCHDOG)

    def exit_port_marker(self, code: str) -> str:
        """Present once the exit watchdog has tried a new WARP port for <code>:
        if it still doesn't handshake, the next step is a restart."""
        return self.exit_marker(code) + ".port"

    def exit_hold(self, code: str) -> str:
        """While it exists, the exit watchdog leaves <code> up (jump test)."""
        return self.exit_marker(code) + ".hold"

    @property
    def change_dir(self) -> str:
        return os.path.join(self.backup_dir, "files")

    def unit_file(self, name: str) -> str:
        return os.path.join(self.systemd_dir, name)


def device_ip(number: int) -> str:
    """A device's static address."""
    return DEVICE_NET.rsplit(".", 1)[0] + f".{number}"


# The settings table (setup's review, status): key, label, default. A row with
# a default is only shown when its value differs from it.
SETTING_ROWS = (
    ("SERVER_NAME", "name", ""), ("ENDPOINT", "endpoint", ""), ("WG_PORT", "port", ""),
    ("WG_SUBNET", "subnet", ""), ("WG_SUBNET6", "subnet6", ""),
    ("WG_DNS", "DNS", ""), ("CLIENT_ALLOWED_IPS", "allowed IPs", ""), ("WG_MTU", "MTU", ""),
    ("WARP_MTU", "WARP MTU", DEFAULTS["WARP_MTU"]), ("WG_IF", "interface", DEFAULTS["WG_IF"]),
    ("WARP_IF", "WARP interface", DEFAULTS["WARP_IF"]), ("RT_TABLE", "table", DEFAULTS["RT_TABLE"]),
)
