#!/usr/bin/env bash
# trial.sh — Full trial on a throwaway server (it gets named bossk), ending where
# it started. Run on the server, as root, from the checkout:
#   tools/trial.sh <jump.conf>   (any single-peer WireGuard config in Paris, e.g. Proton's)
#
# purge -> snapshot -> setup -> test -> jump + configs -> static addresses and groups
# -> device on-demand and fail-closed -> backup -> purge -> restore (same keys)
# -> purge -> diff with the snapshot.
set -uo pipefail
[ $# -eq 1 ] || { echo "usage: $0 <jump.conf>" >&2; exit 2; }
cd "$(dirname "$0")/.."
jump="$1"
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
wait_probe() { local i p; for i in $(seq 1 10); do sleep 3; p=$(probe); [[ $p == *"$1"* ]] && return 0; done; return 1; }  # "loc=FR warp=off"

echo "=== clean start"
W rollback --purge --yes >/dev/null 2>&1
ip netns del dev 2>/dev/null
snap /root/trial-before

echo "=== setup"
DESC="setup succeeds"; ok W setup --name bossk --yes >/root/trial-setup.log 2>&1
DESC="wgt test passes"; ok W test >/root/trial-test.log 2>&1
DESC="watchdog units run wgt"; ok grep -q 'ExecStart=/usr/local/sbin/wgt watchdog warp' /etc/systemd/system/wg-tools-warp-watchdog.service

echo "=== jump and clients"
DESC="jump add par-fr"; ok W jump add par-fr "$jump" --label "Paris, France" --yes >/root/trial-exit.log 2>&1
DESC="client add phone (host + par-fr, plain + warp)"; ok W client add phone --exit host,par-fr --no-qr --yes >/dev/null 2>&1
DESC="four configs"; ok test "$(ls /etc/wireguard/clients/bossk.*phone.conf | wc -l)" = 4
device_up /etc/wireguard/clients/bossk.warp.par-fr.phone.conf
DESC="device exits in Paris (on demand)"; ok wait_loc FR
systemctl stop wg-tools-exit-watchdog.timer; systemctl stop wg-quick@wx-par-fr wg-quick@px-par-fr
DESC="device blocked while the jump is down"; ok test -z "$(probe)"
systemctl start wg-tools-exit-watchdog.timer
DESC="watchdog brings it back"; ok wait_loc FR
device_up /etc/wireguard/clients/bossk.warp.phone.conf
DESC="host + WARP exits via WARP (US)"; ok wait_loc US
DESC="jump test"; ok W jump test >/dev/null 2>&1
DESC="remove refused while used"; ok bash -c '! bin/wgt jump remove par-fr --yes >/dev/null 2>&1'

echo "=== plain configs (no WARP)"
device_up /etc/wireguard/clients/bossk.par-fr.phone.conf
DESC="jump without WARP: Paris, warp=off"; ok wait_probe "loc=FR warp=off"
DESC="rename par-fr.phone -> warp.par-fr.phone2 (live)"; ok W client rename par-fr.phone warp.par-fr.phone2 --yes >/dev/null 2>&1
DESC="same tunnel now through WARP"; ok wait_probe "loc=FR warp=on"
device_up /etc/wireguard/clients/bossk.phone.conf
DESC="host without WARP: warp=off"; ok wait_probe "warp=off"
DESC="host without WARP: no IPv6 through WARP"; ok bash -c '! ip netns exec dev curl -6 -s -m 5 https://[2606:4700:4700::1111]/cdn-cgi/trace | grep -q warp=on'
DESC="wgt test with plain configs"; ok W test >/dev/null 2>&1

echo "=== static addresses and groups"
DESC="phone has a static number"; ok bash -c "bin/wgt device list --tsv | grep -qE '^phone2?\s[0-9]+'"
DESC="group add"; ok W device group phone trial,server --yes >/dev/null 2>&1
DESC="group shows"; ok bash -c "bin/wgt device list --tsv | grep -qE '^phone\s[0-9]+\strial,server'"
sleep 20
DESC="the watchdog lets it reach the server"; ok bash -c "iptables -S WGT-SERVER | grep -q ACCEPT"
DESC="ungroup"; ok W device ungroup phone --yes >/dev/null 2>&1
sleep 20
DESC="isolated again"; ok bash -c "! iptables -S WGT-SERVER | grep -- '-s ' | grep -q ACCEPT"
DESC="wgt test after groups"; ok W test >/dev/null 2>&1

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
DESC="jump back"; ok bash -c 'bin/wgt jump list | grep -q par-fr'
device_up /etc/wireguard/clients/bossk.warp.par-fr.phone.conf
DESC="old device config still exits in Paris"; ok wait_loc FR
ip netns del dev

echo "=== purge and compare"
for config in $(bin/wgt client list --tsv | cut -f1); do W client remove "$config" --yes >/dev/null 2>&1; done
DESC="jump remove"; ok W jump remove par-fr --yes >/dev/null 2>&1
DESC="rollback --purge"; ok W rollback --purge --yes >/dev/null 2>&1
snap /root/trial-after
DESC="box matches the clean snapshot"; ok diff -r /root/trial-before /root/trial-after
diff -r /root/trial-before /root/trial-after | head -20
rm -rf /root/trial-before /root/trial-after /root/trial-archive.tgz
echo ""
echo "    $pass passed, $fail failed (logs: /root/trial-*.log)"
[ "$fail" -eq 0 ]
