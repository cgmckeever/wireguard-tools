#!/bin/bash

SCRIPT_PATH=$(dirname "${BASH_SOURCE[0]}")
source ${SCRIPT_PATH}/color.inc.sh

# ==============================


echo
sudo wg
echo
read -p "Which client do you want to remove? " PUBLIC_KEY

sudo wg set wg0 peer ${PUBLIC_KEY} remove
sed -i "\#${PUBLIC_KEY}#d" ~/authorized-keys.wireguard
echo; echo
sudo wg
echo