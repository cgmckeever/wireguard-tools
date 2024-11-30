#!/bin/bash

SCRIPT_PATH=$(realpath "$(dirname "${BASH_SOURCE[0]}")/")
source ${SCRIPT_PATH}/shared.inc.sh

touch ${CONFIG_PATH}
sudo chmod 755 ${CONFIG_PATH}
source ${CONFIG_PATH}

# ==============================

${SCRIPT_PATH}/system.sh stop

WG_NETWORK_DEFAULT=${WG_NETWORK:-"10.10.0.0"}
prompt "Wireguard Network - default [${WG_NETWORK_DEFAULT}]:" WG_NETWORK
WG_NETWORK=${WG_NETWORK:-${WG_NETWORK_DEFAULT}}
IFS='.' read A B C D <<< ${WG_NETWORK}
WG_CIDR=${A}.${B}.${C}.0/24

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
    prompt "Enter server private key:" PRIVATE_KEY
    store_key ${PRIVATE_KEY}

    printf $success "\nServer Keys Updated\n"
else
    GENKEYS_DEFAULT=y
    if [[ "$(sudo cat /etc/wireguard/privatekey)" != "" && "$(sudo cat /etc/wireguard/publickey)" != "" ]]; then
        GENKEYS_DEFAULT=n
        printf $info "\nFound existing Wireguard keys \n"
        PRIVATE_KEY=$(sudo cat /etc/wireguard/privatekey)
        prompt "Generate new Wireguard keys [y/N]?" GENKEYS
    fi

    GENKEYS=${GENKEYS:-${GENKEYS_DEFAULT}}
    if [[ "${GENKEYS}" =~ ^[Yy]$ ]];then
        PRIVATE_KEY=$(wg genkey)
        store_key ${PRIVATE_KEY}
        printf $success "\nServer Keys Generated\n"
    else
        printf $success "\nExisting Server Keys Used\n"
    fi
fi

PUBLIC_KEY=$(sudo cat /etc/wireguard/publickey)

sudo sed \
    -e "s/__WG_NETWORK/${WG_NETWORK}/g" \
    -e "s#__WG_CIDR#${WG_CIDR}#g" \
    -e "s#__WG_SERVER_PUBLIC_KEY#${PUBLIC_KEY}#g" \
    -e "s/__WG_PORT/${WG_PORT}/g" \
    -e "s#__WG_DEFAULT_ALLOWED_IPS#${WG_ALLOWED_IPS}#g" \
    ${TEMPLATE_PATH}/wireguard-tools.conf.sh.tmpl > ${CONFIG_PATH}

touch ${WG_CONFIG_PATH}
NON_INTERFACE_CONFIG=$(sed '/\[Interface\]/,/^$/d' "${WG_CONFIG_PATH}")
echo ${NON_INTERFACE_CONFIG}
pause
${SCRIPT_PATH}/config.sh ${PRIVATE_KEY} "${NON_INTERFACE_CONFIG}"

${SCRIPT_PATH}/system.sh restart
${SCRIPT_PATH}/system.sh enable

mkdir -p ${CLIENT_PATH}
mkdir -p ${BACKUP_PATH}

printf $success "\nWireguard configured. \n"
printf $success "Create clients using the 'scripts/client-setup.sh' script. \n\n"




