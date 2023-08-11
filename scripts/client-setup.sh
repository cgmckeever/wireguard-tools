#!/bin/bash

SCRIPT_PATH=$(dirname "${BASH_SOURCE[0]}")
source ${SCRIPT_PATH}/shared.inc.sh

# ==============================

NIC=$(route | grep default | awk '{print $8}')
SERVER_IP=$(ip addr show $NIC | grep -m 1 "inet " | awk '{print $2}' | cut -d "/" -f1)

echo; echo
read -p "Whaat is the client name/alias? " WG_ALIAS

printf $info "\n\nWireguard Network is ${WG_NETWORK} \n"
read -p "What IP should the client be allocated? " IP

echo; echo
read -p "What are the allowed IPs? default [${WG_DEFAULT_ALLOWED_IPS}] " ALLOWED_IPS
ALLOWED_IPS=${ALLOWED_IPS:-${WG_DEFAULT_ALLOWED_IPS}}


read -p "Enter an existing Wireguard Private Key: " PRIVATE_KEY
PRIVATE_KEY=${PRIVATE_KEY:-$(wg genkey)}
PUBLIC_KEY=$(echo ${PRIVATE_KEY} | wg pubkey)

cp ${TEMPLATE_PATH}/client.conf.tmpl /tmp/wg-client.conf

sudo sed -i \
    -e "s#__SERVER_IP#${SERVER_IP}#g" \
    -e "s/__PORT/${WG_PORT}/g" \
    -e "s#__SERVER_PUBLIC_KEY#${WG_PUBLIC_KEY}#g" \
    -e "s#__CLIENT_PRIVATE_KEY#${PRIVATE_KEY}#g" \
    -e "s#__IP#${IP}/32#g" \
    -e "s#__ALLOWED_IPS#${ALLOWED_IPS}#g" \
    /tmp/wg-client.conf

printf $info "\n\nWireguard Client Conf: \n"
cat /tmp/wg-client.conf

echo; echo
cat /tmp/wg-client.conf| qrencode -t ansiutf8
rm /tmp/wg-client.conf

sudo wg set wg0 peer ${PUBLIC_KEY} allowed-ips ${IP}/32
echo; echo

echo "${WG_ALIAS} : ${PUBLIC_KEY} : ${IP} : ${ALLOWED_IPS}" >> ~/authorized-keys.wireguard