#!/bin/bash

SCRIPT_PATH=$(realpath "$(dirname "${BASH_SOURCE[0]}")/")
source ${SCRIPT_PATH}/shared.inc.sh

# ==============================

echo
prompt "Enter server private key: " PRIVATE_KEY
echo ${PRIVATE_KEY} | sudo tee /etc/wireguard/privatekey | wg pubkey | sudo tee /etc/wireguard/publickey

printf $info "\n\nServer Keys Updated\n"