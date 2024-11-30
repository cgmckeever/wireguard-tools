#!/bin/bash

SCRIPT_PATH=$(realpath "$(dirname "${BASH_SOURCE[0]}")/")
source ${SCRIPT_PATH}/shared.inc.sh

touch ${CONFIG_PATH}
sudo chmod 755 ${CONFIG_PATH}
source ${CONFIG_PATH}

# ==============================

sudo systemctl stop wg-quick@wg0

WG_NETWORK_DEFAULT=${WG_NETWORK:-"10.10.0.0/24"}
prompt "Wireguard Network - default [${WG_NETWORK_DEFAULT}]:" WG_NETWORK
WG_NETWORK=${WG_NETWORK:-${WG_NETWORK_DEFAULT}}
IFS='.' read A B C D <<< ${WG_NETWORK}
WG_IP_RANGE=${A}.${B}.${C}.0/24

WG_PORT_DEFAULT=${WG_PORT:-51820}
prompt "Wireguard Port - default [${WG_PORT_DEFAULT}]:" WG_PORT
WG_PORT=${WG_PORT:-${WG_PORT_DEFAULT}}
sudo ufw allow ${WG_PORT}/udp

prompt "Default client allowed-ip - default [0.0.0.0/0]:" WG_ALLOWED_IPS
WG_ALLOWED_IPS=${WG_ALLOWED_IPS:-0.0.0.0/0}

sudo touch /etc/wireguard/privatekey
sudo touch /etc/wireguard/publickey

prompt "Import existing Server Key [y/N]?" IMPORT_KEY
if [[ "${IMPORT_KEY}" =~ ^[Yy]$ ]];then
    echo
    prompt "Enter server private key: " PRIVATE_KEY
    store_key ${PRIVATE_KEY}

    printf $success "\n\nServer Keys Updated\n"
else
    echo
    GENKEYS_DEFAULT=y
    if [[ "$(sudo cat /etc/wireguard/privatekey)" != "" && "$(sudo cat /etc/wireguard/publickey)" != "" ]]; then
        GENKEYS_DEFAULT=n
        printf $info "\n\nFound existing Wireguard keys \n"
        PRIVATE_KEY=$(sudo cat /etc/wireguard/privatekey)
        prompt "Generate new Wireguard keys [y/N]?" GENKEYS
    fi

    GENKEYS=${GENKEYS:-${GENKEYS_DEFAULT}}
    if [[ "${GENKEYS}" =~ ^[Yy]$ ]];then
        PRIVATE_KEY=$(wg genkey)
        store_key ${PRIVATE_KEY}
        printf $success "\n\nServer Keys Generated\n"
    else
        printf $success "\n\nExisting Server Keys Used\n"
    fi
fi

PUBLIC_KEY=$(sudo cat /etc/wireguard/publickey)

sudo sed -e "s#__IP_RANGE#${WG_IP_RANGE}#g" \
    -e "s/__PORT/${WG_PORT}/g" \
    -e "s#__PRIVATE_KEY#${PRIVATE_KEY}#g" \
    -e "s/__NIC/${NIC}/g" \
    ${TEMPLATE_PATH}/wg0.conf.tmpl > /etc/wireguard/wg0.conf

printf $info "\nWireguard config: \n"
sudo cat /etc/wireguard/wg0.conf

sudo sed -e "s#__WG_NETWORK#${WG_NETWORK}#g" \
    -e "s#__WG_SERVER_PUBLIC_KEY#${PUBLIC_KEY}#g" \
    -e "s#__WG_PORT#${WG_PORT}#g" \
    -e "s#__WG_DEFAULT_ALLOWED_IPS#${WG_ALLOWED_IPS}#g" \
    ${TEMPLATE_PATH}/wireguard-tools.conf.sh.tmpl > ${CONFIG_PATH}

echo
sudo systemctl restart wg-quick@wg0
sudo systemctl enable wg-quick@wg0

mkdir -p ${CLIENT_PATH}
mkdir -p ${BACKUP_PATH}

printf $success "\n\nWireguard configured. \n"
printf $success "Create clients using the 'scripts/client-setup.sh' script. \n\n"




