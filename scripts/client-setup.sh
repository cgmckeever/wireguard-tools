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
CLIENT_PSK_PATH=${CLIENT_PATH}/${WG_ALIAS}.psk.txt

if [[ -e "${CLIENT_CONF}" ]];then
    PRIVATE_KEY_DEFAULT=$(sed -n 's/PrivateKey = \(.*\)/\1/p' "${CLIENT_CONF}")
    
    IP=$(sed -n 's/Address = \(.*\)/\1/p' "${CLIENT_CONF}")
    IFS='.' read A B C LAST_OCTET_DEFAULT <<< ${IP}
    LAST_OCTET_DEFAULT=$(echo "${LAST_OCTET_DEFAULT}" | sed 's/\/.*//')
    LAST_OCTET_PROMPT="[${LAST_OCTET_DEFAULT}]"

    WG_ALLOWED_IPS_DEFAULT=$(sed -n 's/AllowedIPs = \(.*\)/\1/p' "${CLIENT_CONF}")
fi 

printf $info "\nWireguard Network is ${WG_IPV4_NETWORK}.0"

LAST_OCTET_PROMPT=${LAST_OCTET_PROMPT:-"__"}
prompt "IP to allocate client: ${WG_IPV4_NETWORK}.${LAST_OCTET_PROMPT}" LAST_OCTET
LAST_OCTET=${LAST_OCTET:-${LAST_OCTET_DEFAULT}}
IPV4="${WG_IPV4_NETWORK}.${LAST_OCTET}/32"
IPV6="${WG_IPV6_NETWORK}::${LAST_OCTET}/128"
ADDRESS="${IPV4},${IPV6}"

printf $info "\nStandard Allowed-IP Routes:"
printf $warn "\n  All Traffic: 0.0.0.0/0,::/0"
printf $warn "\n  Wireguard Network: ${WG_IPV4_NETWORK}.0/24,${WG_IPV6_NETWORK}::/64\n"

WG_ALLOWED_IPS_DEFAULT=${WG_ALLOWED_IPS_DEFAULT:-${WG_DEFAULT_ALLOWED_IPS}}
prompt "Allowed IPs - default [${WG_ALLOWED_IPS_DEFAULT}]:" WG_ALLOWED_IPS
WG_ALLOWED_IPS=${WG_ALLOWED_IPS:-${WG_ALLOWED_IPS_DEFAULT}}

PRIVATE_KEY_PROMPT=${PRIVATE_KEY_DEFAULT:-"enter to create new key-pair"}
PRIVATE_KEY_DEFAULT=${PRIVATE_KEY_DEFAULT:-$(wg genkey)}
prompt "Enter an existing Wireguard Private Key [${PRIVATE_KEY_PROMPT}]:" PRIVATE_KEY
PRIVATE_KEY=${PRIVATE_KEY:-${PRIVATE_KEY_DEFAULT}}
PUBLIC_KEY=$(echo ${PRIVATE_KEY} | wg pubkey)

CLIENT_PSK=$(wg genpsk)
echo ${CLIENT_PSK} > ${CLIENT_PSK_PATH}
sudo chmod 600 ${CLIENT_PSK_PATH}

sudo wg set wg0 peer ${PUBLIC_KEY} \
    allowed-ips "${ADDRESS}" \
    preshared-key ${CLIENT_PSK_PATH}

sudo sed \
    -e "s#__WG_SERVER_IP#${SERVER_IP}#g" \
    -e "s/__WG_PORT/${WG_PORT}/g" \
    -e "s#__WG_SERVER_PUBLIC_KEY#${WG_SERVER_PUBLIC_KEY}#g" \
    -e "s#__WG_CLIENT_PRIVATE_KEY#${PRIVATE_KEY}#g" \
    -e "s#__WG_CLIENT_PUBLIC_KEY#${PUBLIC_KEY}#g" \
    -e "s#__WG_ADDRESS#${ADDRESS}#g" \
    -e "s#__WG_ALLOWED_IPS#${WG_ALLOWED_IPS}#g" \
    -e "s#__WG_PRESHARED_KEY#${CLIENT_PSK}#g" \
    ${TEMPLATE_PATH}/client.conf.tmpl > ${CLIENT_CONF}

${SCRIPT_PATH}/client-conf.sh ${WG_ALIAS}