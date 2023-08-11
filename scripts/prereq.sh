#!/bin/bash

SCRIPT_PATH=$(dirname "${BASH_SOURCE[0]}")
source ${SCRIPT_PATH}/shared.inc.sh

# ==============================

sudo apt update
sudo apt -y install vim ufw wireguard qrencode

echo net.ipv4.ip_forward=1 | sudo tee -a /etc/sysctl.conf
sudo sysctl -p

sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 51820/udp
sudo ufw allow OpenSSH
sudo ufw enable
sudo ufw status

printf $info "\n\nWireguard prereqs installed. \n"
printf $info "Run the './setup.sh' script to configure. \n"