#!/bin/bash

SCRIPT_PATH=$(dirname "${BASH_SOURCE[0]}")
source ${SCRIPT_PATH}/color.inc.sh

# ==============================

NIC=$(route | grep default | awk '{print $8}')
SERVER_IP=$(ip addr show $NIC | grep -m 1 "inet " | awk '{print $2}' | cut -d "/" -f1)

printf $info "\n\nWireguard Network is ${WG_NETWORK} \n"
read -p "What IP should the client be allocated? " IP

PRIVATE_KEY=$(wg genkey)
PUBLIC_KEY=$(echo ${PRIVATE_KEY} | wg pubkey)

cp ${TEMPLATE_PATH}/client.conf.tmpl /tmp/wg-client.conf

sudo sed -i \
    -e "s#__SERVER_IP#${SERVER_IP}#g" \
    -e "s/__PORT/${WG_PORT}/g" \
    -e "s#__SERVER_PUBLIC_KEY#${WG_PUBLIC_KEY}#g" \
    -e "s#__CLIENT_PRIVATE_KEY#${PRIVATE_KEY}#g" \
    -e "s#__IP#${IP}/32#g" \
    -e "s/__ALLOWED_IPS/${ALLOWED_IPS}/g" \
    /tmp/wg-client.conf


printf $info "\n\nWireguard Client Conf. \n"
cat /tmp/wg-client.conf

cat /tmp/wg-client.conf| qrencode -t ansiutf8
rm /tmp/wg-client.conf

sudo wg set wg0 peer ${PUBLIC_KEY} allowed-ips ${IP}/32