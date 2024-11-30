#!/bin/bash

SCRIPT_PATH=$(realpath "$(dirname "${BASH_SOURCE[0]}")/")
source ${SCRIPT_PATH}/shared.inc.sh
source ${CONFIG_PATH}

# =================

CLIENT_CONF=${CLIENT_PATH}/${1}.conf
PRIVATE_KEY=$(grep -oP '(?<=^PrivateKey = ).*' ${CLIENT_CONF})
PUBLIC_KEY=$(echo ${PRIVATE_KEY} | wg pubkey)
IP=$(grep -oP '(?<=^Address = ).*' ${CLIENT_CONF})

sudo wg set wg0 peer ${PUBLIC_KEY} allowed-ips ${IP}

printf $info "\nWireguard Client Conf: \n"
cat ${CLIENT_CONF}

echo; echo
cat ${CLIENT_CONF} | qrencode -t ansiutf8