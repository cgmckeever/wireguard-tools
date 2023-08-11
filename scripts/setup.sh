#!/bin/bash

SCRIPT_PATH=$(dirname "${BASH_SOURCE[0]}")
source ${SCRIPT_PATH}/shared.inc.sh

# ==============================

sudo systemctl stop wg-quick@wg0

NIC=$(route | grep default | awk '{print $8}')

echo; echo
WG_NETWORK=${WG_NETWORK:-"10.10.0.0/24"}
read -p "What is the Wireguard Network? default [${WG_NETWORK}] " NETWORK
NETWORK=${NETWORK:-${WG_NETWORK}}
IFS='.' read A B C D <<< ${NETWORK}
IP=${A}.${B}.${C}.1/24

echo; echo
WG_PORT=${WG_PORT:-51820}
read -p "What port should Wireguard run on? default [${WG_PORT}] " PORT
PORT=${PORT:-${WG_PORT}}

echo; echo
read -p "Default client allowed-ips? default [0.0.0.0/0] " ALLOWED_IPS
ALLOWED_IPS=${ALLOWED_IPS:-0.0.0.0/0}

GENKEYS=y

sudo touch /etc/wireguard/privatekey
sudo touch /etc/wireguard/publickey

if [[ "$(cat /etc/wireguard/privatekey)" != "" && "$(cat /etc/wireguard/publickey)" != "" ]]; then
    printf $info "\n\nFound existing keys \n"
    read -p "Generate new Wireguard keys [Y/n]? " GENKEYS
    GENKEYS=${GENKEYS:-"y"}
fi

if [[ "${GENKEYS}" =~ ^[Yy]$ ]];then
    wg genkey | sudo tee /etc/wireguard/privatekey | wg pubkey | sudo tee /etc/wireguard/publickey
fi

PRIVATE_KEY=$(cat /etc/wireguard/privatekey)
PUBLIC_KEY=$(cat /etc/wireguard/publickey)

sudo cp ${TEMPLATE_PATH}/wg0.conf.tmpl /etc/wireguard/wg0.conf

sudo sed -i \
    -e "s#__IP#${IP}#g" \
    -e "s/__PORT/${PORT}/g" \
    -e "s#__PRIVATE_KEY#${PRIVATE_KEY}#g" \
    -e "s/__NIC/${NIC}/g" \
    /etc/wireguard/wg0.conf

sudo cat /etc/wireguard/wg0.conf

sudo cp ${TEMPLATE_PATH}/wireguard.profile.sh.tmpl /etc/profile.d/wireguard.profile.sh

sudo sed -i \
    -e "s#__WG_NETWORK#${NETWORK}#g" \
    -e "s#__WG_PUBLIC_KEY#${PUBLIC_KEY}#g" \
    -e "s#__WG_PORT#${PORT}#g" \
    -e "s#__DEFAULT_ALLOWED_IPS#${ALLOWED_IPS}#g" \
    /etc/profile.d/wireguard.profile.sh

sudo chmod 755 /etc/profile.d/wireguard.profile.sh

sudo systemctl start wg-quick@wg0
sudo systemctl enable wg-quick@wg0

touch ~/authorized-keys.wireguard

printf $info "\n\nWireguard configured. \n"
printf $info "Create clients using the './client-setup.sh' script. \n\n"





