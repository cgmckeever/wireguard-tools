#!/usr/bin/env bash
# trial.sh — Full trial of the Python version on a throwaway server (bossk),
# ending where it started. Run on the server, as root, from the checkout:
#   tools/trial.sh <jump.conf>   (any single-peer WireGuard config in Paris, e.g. Proton's)
#
# purge -> snapshot -> setup -> test -> jump + clients
# -> device on-demand and fail-closed -> backup -> purge -> restore (same keys)
# -> warp off/on -> purge -> diff with the snapshot.
set -uo pipefail
[ $# -eq 1 ] || { echo "usage: $0 <jump.conf>" >&2; exit 2; }
cd "$(dirname "$0")/.."
proton="$1"
W() { bin/wgt "$@"; }
pass=0 fail=0
exec 3>&1   # results go to fd 3, so a check's own output can be redirected to a log
ok() { if "$@"; then echo "  PASS $DESC" >&3; pass=$((pass + 1)); else echo "  FAIL $DESC" >&3; fail=$((fail + 1)); fi; }
snap() {
    mkdir -p "$1"
    iptables-save | grep -v '^#' | sed 's/\[[0-9:]*\]//' >"$1/iptables"
    ip6tables-save | grep -v '^#' | sed 's/\[[0-9:]*\]//' >"$1/ip6tables"
    ip rule >"$1/rule"; ip -6 rule >"$1/rule6"
    ip route >"$1/route"; ip -6 route | grep -v expires >"$1/route6"
    ip -br link | awk '{print $1}' | sort >"$1/links"
    systemctl list-unit-files --no-legend 'wg-*' 'wg-quick*' | awk '{print $1, $2}' | sort >"$1/units"
    ls /etc/sysctl.d /usr/local/sbin /etc/systemd/system 2>/dev/null >"$1/files"
    ls -d /etc/wireguard /var/backups/wireguard-tools 2>/dev/null >"$1/dirs"
}
device_up() {   # $1 = client config: a simulated device in netns "dev"
    local conf="$1"
    get() { sed -n "s/^$1[[:space:]]*=[[:space:]]*//p" "$conf" | head -1; }
    ip netns del dev 2>/dev/null; ip link del cwg 2>/dev/null
    ip netns add dev; ip -n dev link set lo up; ip link add cwg type wireguard
    wg set cwg private-key <(get PrivateKey) peer "$(get PublicKey)" preshared-key <(get PresharedKey) \
        endpoint "$(get Endpoint)" allowed-ips 0.0.0.0/0,::/0 persistent-keepalive 25
    ip link set cwg netns dev
    for a in $(get Address | tr -d ' ' | tr , ' '); do ip -n dev addr add "$a" dev cwg; done
    ip -n dev link set cwg mtu 1280 up; ip -n dev route add default dev cwg; ip -n dev -6 route add default dev cwg
}
probe() { ip netns exec dev curl -s -m 5 https://1.1.1.1/cdn-cgi/trace | grep -E '^(loc|warp)=' | tr '\n' ' '; }
wait_loc() { local i p; for i in $(seq 1 10); do sleep 3; p=$(probe); [[ $p == *"loc=$1"* ]] && return 0; done; return 1; }

echo "=== clean start"
W rollback --purge --yes >/dev/null 2>&1
ip netns del dev 2>/dev/null
snap /root/trial-before

echo "=== setup"
DESC="setup succeeds"; ok W setup --name bossk --yes >/root/trial-setup.log 2>&1
DESC="wgt test passes"; ok W test >/root/trial-test.log 2>&1
DESC="watchdog units run wgt"; ok grep -q 'ExecStart=/usr/local/sbin/wgt watchdog warp' /etc/systemd/system/wg-tools-warp-watchdog.service

echo "=== jump and clients"
DESC="exit add par-fr"; ok W exit add par-fr "$proton" --label "Paris, France" --yes >/root/trial-exit.log 2>&1
DESC="client add phone (warp + par-fr)"; ok W client add phone --exit warp,par-fr --no-qr --yes >/dev/null 2>&1
device_up /etc/wireguard/clients/bossk.par-fr.phone.conf
DESC="device exits in Paris (on demand)"; ok wait_loc FR
systemctl stop wg-tools-exit-watchdog.timer; systemctl stop wg-quick@wx-par-fr wg-quick@px-par-fr
DESC="device blocked while the jump is down"; ok test -z "$(probe)"
systemctl start wg-tools-exit-watchdog.timer
DESC="watchdog brings it back"; ok wait_loc FR
device_up /etc/wireguard/clients/bossk.phone.conf
DESC="default tunnel exits via WARP (US)"; ok wait_loc US
DESC="exit test"; ok W exit test >/dev/null 2>&1
DESC="remove refused while used"; ok bash -c '! bin/wgt exit remove par-fr --yes >/dev/null 2>&1'

echo "=== backup, purge, restore"
ip netns del dev
keys_before=$(md5sum /etc/wireguard/server.key /etc/wireguard/clients/*.conf | md5sum)
W backup --yes >/dev/null 2>&1
archive=$(ls -t /var/backups/wireguard-tools/wireguard-tools.*.tar.gz | head -1)
cp "$archive" /root/trial-archive.tgz
W rollback --purge --yes >/dev/null 2>&1
DESC="purge leaves no wg0"; ok bash -c '! ip link show wg0 >/dev/null 2>&1'
DESC="restore succeeds"; ok W restore /root/trial-archive.tgz --yes >/root/trial-restore.log 2>&1
DESC="same keys after restore"; ok test "$keys_before" = "$(md5sum /etc/wireguard/server.key /etc/wireguard/clients/*.conf | md5sum)"
DESC="jump back"; ok bash -c 'bin/wgt exit list | grep -q par-fr'
device_up /etc/wireguard/clients/bossk.par-fr.phone.conf
DESC="old device config still exits in Paris"; ok wait_loc FR
ip netns del dev

echo "=== warp off/on"
DESC="warp off"; ok W warp off --yes >/dev/null 2>&1
DESC="test with WARP off"; ok W test >/dev/null 2>&1
DESC="warp on"; ok W warp on --yes >/dev/null 2>&1
DESC="test with WARP on"; ok W test >/dev/null 2>&1

echo "=== purge and compare"
W client remove par-fr.phone --yes >/dev/null 2>&1; W client remove phone --yes >/dev/null 2>&1
DESC="exit remove"; ok W exit remove par-fr --yes >/dev/null 2>&1
DESC="rollback --purge"; ok W rollback --purge --yes >/dev/null 2>&1
snap /root/trial-after
DESC="box matches the clean snapshot"; ok diff -r /root/trial-before /root/trial-after
diff -r /root/trial-before /root/trial-after | head -20
rm -rf /root/trial-before /root/trial-after /root/trial-archive.tgz
echo ""
echo "    $pass passed, $fail failed (logs: /root/trial-*.log)"
[ "$fail" -eq 0 ]
