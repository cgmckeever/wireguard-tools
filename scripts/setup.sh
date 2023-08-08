#!/bin/bash

SCRIPT_PATH=$(dirname "${BASH_SOURCE[0]}")
source ${SCRIPT_PATH}/color.inc.sh

# ==============================

NIC=$(route | grep default | awk '{print $8}')

read -p "What is the Wireguard Network? default [10.10.0.0/24]" NETWORK
read A B C D <<<"${NETWORK//./ }"
IP=${A}.${B}.${C}.1
echo; echo
read -p "What port should Wireguard run on? default [51820]" PORT

GENKEYS=y

if [[ "$(cat /etc/wireguard/privatekey)" == "" && "$(cat /etc/wireguard/publickey)" == "" ]]; then
    read -p "Generate new Wireguard keys [Y/n]?" GENKEYS
fi

if [[ "${GENKEYS}" =~ ^[Yy]$ ]];then
    wg genkey | sudo tee /etc/wireguard/privatekey | wg pubkey | sudo tee /etc/wireguard/publickey
fi

PRIVATE_KEY=$(cat /etc/wireguard/privatekey)
PUBLIC_KEY=$(cat /etc/wireguard/publickey)

sudo cp ${TEMPLATE_PATH}/wg0.conf.tmpl /etc/wireguard/wg0.conf

sudo sed -i \
    -e "s/__IP/${IP}/g" \
    -e "s/__PORT/${PORT}/g" \
    -e "s/__PRIVATE_KEY/${PRIVATE_KEY}/g" \
    -e "s/__NIC/${NIC}/g" \
    /etc/wireguard/wg0.conf

#sudo systemctl start wg-quick@wg0
#sudo systemctl enable wg-quick@wg0





