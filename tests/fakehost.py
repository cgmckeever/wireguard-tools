"""A fake host that changes as commands run: routes, rules, firewall chains and
peers, and wg0 really going away when stopped. FakeRunner answers every command
the same way however often it runs, so it can't show what's true afterwards;
this can. It reads the same shell lines wg0.conf holds, and only models what we
understand `ip`, `iptables` and wg-quick to do: tools/trial.sh is the real check.

A Host can also die at its Nth change, and everything after that fails too, as
when a process is killed: `die_at`."""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
from typing import Dict, List, Optional, Sequence, Set, Tuple

from wgt.errors import CommandFailed
from wgt.system import FakeRunner, Out

BUILTIN = {"INPUT", "FORWARD", "OUTPUT", "PREROUTING", "POSTROUTING"}
FIXTURE_PROFILE = os.path.join(os.path.dirname(__file__), "fixtures", "exits", "wg", "wgcf", "wgcf-profile.conf")


class Died(BaseException):
    """The process was killed here. Not an Exception, so nothing handles it."""


class Kernel:
    """What's running. Survives a process dying, so it's outside the runner."""

    def __init__(self) -> None:
        self.rules: List[Tuple[str, str]] = []
        self.routes: Set[Tuple[str, str, str]] = set()
        self.chains: Dict[Tuple[str, str, str], List[str]] = {}
        self.peers: Dict[str, str] = {}
        self.links: Set[str] = {"warp"}
        self.addrs: List[str] = []
        self.has_key = False
        self.ufw_active = False
        self.ufw: Set[str] = set()     # "<port>/udp" and "route <in> <out>"

    def view(self, chain_contents: bool = False) -> dict:
        """Comparable state. The device chains' rules are the watchdog's, not wg0.conf's."""
        chains = {k: sorted(v) for k, v in self.chains.items() if v or k[2].startswith("WGT")}
        if not chain_contents:
            chains = {k: [r for r in v if not (k[2].startswith("WGT") and r.startswith(("-s ", "-d ")))]
                      for k, v in chains.items()}
        return {"rules": sorted(self.rules), "routes": sorted(self.routes), "chains": chains,
                "peers": dict(self.peers)}


class Host(FakeRunner):
    """A Runner whose commands change a Kernel."""

    def __init__(self, kernel: Kernel, wg_dir: str, die_at: Optional[int] = None, handshakes=None, dry_run=False):
        quiet = io.StringIO()
        super().__init__({}, dry_run=dry_run, out=Out(quiet, quiet))
        self.kernel = kernel
        self.wg_dir = wg_dir
        self.die_at = die_at
        self.steps = 0
        self.handshakes = handshakes or {}
        self.keys = 0

    # --- dying part-way ---

    def tick(self) -> None:
        """One change is about to happen."""
        self.steps += 1
        if self.die_at is not None and self.steps >= self.die_at:
            raise Died(f"step {self.steps}")

    def run(self, args, check=True, input=None, capture=False):
        if not self.dry_run:
            self.tick()
        return super().run(args, check, input, capture)

    def attempt(self, args):
        if not self.dry_run:
            self.tick()
        return super().attempt(args)

    def sh(self, line, check=True):
        if not self.dry_run:
            self.tick()
        return super().sh(line, check)

    def run_logged(self, args, log, cwd, append=False):
        if args[1:2] == ["register"]:
            with open(os.path.join(cwd, "wgcf-account.toml"), "w") as f:
                f.write("account\n")
        if args[1:2] == ["generate"]:
            shutil.copy(FIXTURE_PROFILE, cwd)
        return True

    # --- the commands ---

    def _exec(self, args: Sequence[str], check: bool, input=None, capture: bool = False) -> Tuple[int, str]:
        self.calls.append(" ".join(args))
        rc, stdout = self._answer(list(args), input)
        if check and rc != 0:
            raise CommandFailed(f"command failed (exit {rc}): {' '.join(args)}")
        return rc, stdout

    def _answer(self, a: List[str], input) -> Tuple[int, str]:
        k, c = self.kernel, " ".join(a)
        if a[:2] == ["bash", "-c"]:
            return self.shell_line(a[2]), ""
        if c == "wg genkey":
            self.keys = self.keys + 1
            return 0, f"PRIV{os.urandom(4).hex()}=\n"
        if c == "wg genpsk":
            return 0, f"PSK{os.urandom(4).hex()}=\n"
        if c == "wg pubkey":
            return 0, (input or "x").strip().replace("PRIV", "PUB") + "\n"
        if c.startswith("ip link show dev"):
            return (0 if a[4] in k.links else 1), ""
        if a[:2] == ["wg", "set"]:
            if a[3:4] == ["peer"] and a[5:] == ["remove"]:
                k.peers.pop(a[4], None)
            elif a[3:4] == ["peer"]:
                k.peers[a[4]] = a[a.index("allowed-ips") + 1]
            return 0, ""
        if a[:2] == ["wg", "show"]:
            if a[3] == "latest-handshakes" and a[2] == "wg0":
                return 0, "".join(f"{p}\t{self.handshakes.get(p, 0)}\n" for p in k.peers)
            if a[3] == "allowed-ips" and a[2] == "wg0":
                return 0, "".join(f"{p}\t{v}\n" for p, v in k.peers.items())
            if a[3] == "listen-port":
                return 0, "51820\n"
            return 0, ""
        if a[0] in ("iptables", "ip6tables") and a[1] == "-t":
            return self._iptables(a)
        if re.fullmatch(r"ip( -6)? rule show", c):
            return 0, self._rule_show("ip -6" if "-6" in c else "ip")
        m = re.fullmatch(r"(ip(?: -6)?) route show table (\S+)", c)
        if m:
            return 0, "".join(x[2] + "\n" for x in sorted(k.routes) if x[0] == m.group(1) and x[1] == m.group(2))
        if c == "ip -4 route show default":
            with open(os.path.join(self.wg_dir, "wg-tools.state")) as f:
                line = next((x for x in f if x.startswith("DEFAULT_ROUTE=")), "DEFAULT_ROUTE=")
            return 0, line.split("=", 1)[1]
        if c.startswith("ip -4 -o addr show dev wg0"):
            return 0, "".join(f"5: wg0 inet {x} \n" for x in k.addrs if ":" not in x)
        if a[0] == "tar":     # the real thing: backups and restores are what's being tested
            return subprocess.run(a, capture_output=True, text=True).returncode, ""
        if a[0] == "ufw":
            return self._ufw(a)
        if a[0] == "curl" and any("cdn-cgi/trace" in x for x in a):
            return 0, f"ip=203.0.113.9\nwarp={'on' if '--interface' in a else 'off'}\n"
        if a[0] == "systemctl" and any(x.startswith("wg-quick@wg0") for x in a):
            return self._wg0_unit(a)
        return 0, ""

    def _ufw(self, a: List[str]) -> Tuple[int, str]:
        k = self.kernel
        if a[1] == "status":
            return 0, "Status: active\n" if k.ufw_active else "Status: inactive\n"
        if a[1:2] == ["allow"]:
            k.ufw.add(a[2])
        elif a[1:3] == ["delete", "allow"]:
            k.ufw.discard(a[3])
        elif a[1:3] == ["route", "allow"]:
            k.ufw.add(f"route {a[5]} {a[8]}")
        elif a[1:4] == ["route", "delete", "allow"]:
            k.ufw.discard(f"route {a[6]} {a[9]}")
        return 0, ""

    def _wg0_unit(self, a: List[str]) -> Tuple[int, str]:
        conf = self.conf()
        verb = a[1]
        stops = verb in ("stop", "restart") or (verb == "disable" and "--now" in a)
        starts = verb in ("start", "restart") or (verb == "enable" and "--now" in a)
        if stops and "wg0" in self.kernel.links:
            self.wg0_down(conf)
        if starts and "wg0" not in self.kernel.links:
            self.wg0_up(conf)
        if verb == "is-active":
            return (0 if "wg0" in self.kernel.links else 3), ""
        return 0, ""

    def conf(self) -> str:
        with open(os.path.join(self.wg_dir, "wg0.conf")) as f:
            return f.read()

    def wg0_up(self, conf: str) -> None:
        """What wg-quick up does: address and peers, then PostUp in order."""
        for m in re.finditer(r"PublicKey = (\S+)\n(?:PresharedKey = \S+\n)?AllowedIPs = (.*)", conf):
            self.kernel.peers[m.group(1)] = m.group(2).replace(" ", "")
        for line in conf.splitlines():
            if line.startswith("Address = "):
                self.kernel.addrs = [x.strip() for x in line[10:].split(",")]
        self.kernel.links.add("wg0")
        for line in conf.splitlines():
            if line.startswith("PostUp = "):
                self.shell_line(line[9:].replace("%i", "wg0"))

    def wg0_down(self, conf: str) -> None:
        for line in conf.splitlines():
            if line.startswith("PostDown = "):
                self.shell_line(line[11:].replace("%i", "wg0"))
        self.kernel.links.discard("wg0")
        self.kernel.peers = {}
        self.kernel.addrs = []
        self.kernel.has_key = False

    def _iptables(self, a: List[str]) -> Tuple[int, str]:
        k = self.kernel
        cmd, table, op = a[0], a[2], a[3]
        chain = a[4] if len(a) > 4 else ""
        spec, key = " ".join(a[5:]), (cmd, table, chain)
        if op == "-S":
            if key not in k.chains and chain not in BUILTIN:
                return 1, ""
            head = f"-N {chain}\n" if chain not in BUILTIN else f"-P {chain} ACCEPT\n"
            return 0, head + "".join(f"-A {chain} {r}\n" for r in k.chains.get(key, []))
        if op == "-C":
            return (0 if spec in k.chains.get(key, []) else 1), ""
        if op == "-A":
            k.chains.setdefault(key, []).append(spec)
        elif op == "-I":
            k.chains.setdefault(key, []).insert(0, spec)
        elif op == "-D":
            if spec not in k.chains.get(key, []):
                return 1, ""
            k.chains[key].remove(spec)
        return 0, ""

    def _rule_show(self, family: str) -> str:
        out = ""
        for fam, spec in self.kernel.rules:
            if fam != family:
                continue
            m = re.search(r"priority (\d+)", spec)
            words = spec.replace(m.group(0), "").split()
            text = re.sub(r"/(32|128)\b", "", " ".join(words))
            if words[0] == "fwmark":
                text = f"from all fwmark {int(words[1]):#x} {' '.join(words[2:])}"
            elif words[0] == "iif":
                text = f"from all to {words[3]} iif {words[1]} {' '.join(words[4:])}"
            out += f"{m.group(1)}:\t{text}\n"
        return out

    def shell_line(self, line: str) -> int:
        """One of the shell lines rules.py writes. An unknown shape is an error, so
        a new kind of hook line can't be silently ignored by these tests."""
        k, line = self.kernel, line.strip()
        m = re.fullmatch(r"while (ip(?: -6)?) rule del (.*?) 2>/dev/null; do :; done(?:; \1 rule add (.*))?", line)
        if m:
            family, spec, add = m.groups()
            k.rules = [x for x in k.rules if x != (family, spec)]
            if add:
                k.rules.append((family, add))
            return 0
        m = re.fullmatch(r"(ip(?: -6)?) rule add (.*?)(?: 2>/dev/null \|\| true)?", line)
        if m:
            if (m.group(1), m.group(2)) not in k.rules:
                k.rules.append((m.group(1), m.group(2)))
            return 0
        m = re.fullmatch(r"(ip(?: -6)?) route (replace|del) (.*?) table (\S+)( 2>/dev/null \|\| true)?", line)
        if m:
            family, verb, what, table, soft = m.groups()
            dev = re.search(r"dev (\S+)", what)
            if verb == "replace":
                if dev and dev.group(1) not in k.links and dev.group(1) != "wg0":
                    return 0 if soft else 1
                k.routes.add((family, table, what))
            else:
                k.routes.discard((family, table, what))
            return 0
        m = re.fullmatch(r"(ip(?: -6)?) route flush table (\S+)( 2>/dev/null \|\| true)?", line)
        if m:
            k.routes = {x for x in k.routes if not (x[0] == m.group(1) and x[1] == m.group(2))}
            return 0
        m = re.fullmatch(r"(ip6?tables) -t (\S+) -C (\S+) (.*?) 2>/dev/null \|\| \1 -t \2 -I \3 \4", line)
        if m:
            cmd, table, chain, rule = m.groups()
            rows = k.chains.setdefault((cmd, table, chain), [])
            if rule not in rows:
                rows.insert(0, rule)
            return 0
        m = re.fullmatch(r"while (ip6?tables) -t (\S+) -C (\S+) (.*?) 2>/dev/null; do .*", line)
        if m:
            cmd, table, chain, rule = m.groups()
            rows = k.chains.get((cmd, table, chain), [])
            while rule in rows:
                rows.remove(rule)
            return 0
        m = re.fullmatch(r"(ip6?tables) -t (\S+) -([NFX]) (\S+) 2>/dev/null \|\| true", line)
        if m:
            cmd, table, op, chain = m.groups()
            key = (cmd, table, chain)
            if op == "N":
                k.chains.setdefault(key, [])
            elif op == "F" and key in k.chains:
                k.chains[key] = []
            elif op == "X":
                k.chains.pop(key, None)
            return 0
        m = re.fullmatch(r"wg set (\S+) private-key (\S+)", line)
        if m:
            k.has_key = True
            return 0
        if line.startswith("sysctl"):
            return 0
        raise AssertionError(f"fakehost doesn't know this shell line: {line}")
