#!/bin/bash

SCRIPT_PATH=$(realpath "$(dirname "${BASH_SOURCE[0]}")/")
source ${SCRIPT_PATH}/shared.inc.sh

# ==============================

sudo apt update
sudo apt -y install net-tools vim ufw wireguard qrencode zip

echo net.ipv4.ip_forward=1 | sudo tee -a /etc/sysctl.conf
sudo sysctl -p

sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw enable
sudo ufw status

printf $success "\n\nWireguard prereqs installed. \n"
printf $success "Run the 'scripts/setup.sh' script to configure. \n\n"