#!/bin/bash

SCRIPT_PATH=$(realpath "$(dirname "${BASH_SOURCE[0]}")/")
source ${SCRIPT_PATH}/shared.inc.sh
source ${CONFIG_PATH}

# ==============================

SERVER_IP=$(ip addr show $NIC | grep -m 1 "inet " | awk '{print $2}' | cut -d "/" -f1)

WG_ALIAS=${1}
if [ -z "${WG_ALIAS}" ]; then
    prompt "Client name/alias:" WG_ALIAS
fi

CLIENT_CONF=${CLIENT_PATH}/${WG_ALIAS}.conf

if [[ -e "${CLIENT_CONF}" ]];then
    PRIVATE_KEY_DEFAULT=$(sed -n 's/PrivateKey = \(.*\)/\1/p' "$file_path")
    
    IP=$(sed -n 's/Address = \(.*\)/\1/p' "$file_path")
    IFS='.' read A B C LAST_OCTET_DEFAULT <<< ${IP}
    LAST_OCTET_PROMPT="[${LAST_OCTET_DEFAULT}]"

    WG_ALLOWED_IPS_DEFAULT=$(sed -n 's/AllowedIPs = \(.*\)/\1/p' "$file_path")
fi 
    printf $info "\nWireguard Network is ${WG_NETWORK}"
    IFS='.' read A B C D <<< ${WG_NETWORK}

    prompt "IP to allocate client: ${A}.${B}.${C}.${LAST_OCTET_PROMPT}" LAST_OCTET
    LAST_OCTET=${LAST_OCTET:-${LAST_OCTET_DEFAULT}}
    IP=${A}.${B}.${C}.${LAST_OCTET}

    WG_ALLOWED_IPS_DEFAULT=${WG_ALLOWED_IPS_DEFAULT:-${WG_DEFAULT_ALLOWED_IPS}}
    prompt "Allowed IPs - default [${WG_ALLOWED_IPS_DEFAULT}]:" WG_ALLOWED_IPS
    WG_ALLOWED_IPS=${WG_ALLOWED_IPS:-${WG_ALLOWED_IPS_DEFAULT}}

    PRIVATE_KEY_PROMPT=${PRIVATE_KEY_DEFAULT:-"enter to create new key-pair"}
    PRIVATE_KEY_DEFAULT=${PRIVATE_KEY_DEFAULT:-$(wg genkey)}
    prompt "Enter an existing Wireguard Private Key [${PRIVATE_KEY_DEFAULT}]:" PRIVATE_KEY
    PRIVATE_KEY=${PRIVATE_KEY:-${PRIVATE_KEY_DEFAULT}}
    PUBLIC_KEY=$(echo ${PRIVATE_KEY} | wg pubkey)

    sudo sed \
        -e "s#__WG_SERVER_IP#${SERVER_IP}#g" \
        -e "s/__WG_PORT/${WG_PORT}/g" \
        -e "s#__WG_SERVER_PUBLIC_KEY#${WG_SERVER_PUBLIC_KEY}#g" \
        -e "s#__WG_CLIENT_PRIVATE_KEY#${PRIVATE_KEY}#g" \
        -e "s#__WG_IP#${IP}/32#g" \
        -e "s#__WG_ALLOWED_IPS#${WG_ALLOWED_IPS}#g" \
        ${TEMPLATE_PATH}/client.conf.tmpl > ${CLIENT_CONF}
else
    sed -i "/^\[Peer\]/,/\[/s/^PublicKey = .*/PublicKey = ${WG_SERVER_PUBLIC_KEY}/" "${CLIENT_CONF}"
fi 

${SCRIPT_PATH}/client-conf.sh ${WG_ALIAS}