#!/bin/bash

SCRIPT_PATH=$(realpath "$(dirname "${BASH_SOURCE[0]}")/")
source ${SCRIPT_PATH}/shared.inc.sh

touch ${CONFIG_PATH}
sudo chmod 755 ${CONFIG_PATH}
source ${CONFIG_PATH}

# ==============================

${SCRIPT_PATH}/system.sh stop
sudo ip link delete wg0 2>/dev/null

WG_NETWORK_DEFAULT=${WG_NETWORK:-"10.10.0.0"}
prompt "Wireguard Network - default [${WG_NETWORK_DEFAULT}]:" WG_IPV4_NETWORK
WG_IPV4_NETWORK=${WG_IPV4_NETWORK:-${WG_NETWORK_DEFAULT}}
IFS='.' read A B C D <<< ${WG_IPV4_NETWORK}

WG_IPV4_NETWORK=${A}.${B}.${C}
WG_IPV4="${WG_IPV4_NETWORK}.1/24"

WG_IPV6_NETWORK="fd$(printf "%02x" $((RANDOM % 256))):$(printf "%04x" $((RANDOM % 65536))):$(printf "%04x" $((RANDOM % 65536)))"
WG_IPV6="${WG_IPV6_NETWORK}::1/64"

WG_PORT_DEFAULT=${WG_PORT:-51820}
prompt "Wireguard Port - default [${WG_PORT_DEFAULT}]:" WG_PORT
WG_PORT=${WG_PORT:-${WG_PORT_DEFAULT}}
sudo ufw allow ${WG_PORT}/udp

printf $info "\nConfigure Default Routing Rules:\n"

prompt "Do you want to restrict traffic to the Wireguard Network [Y/n]?" TRAFFIC
if [[ "${TRAFFIC}" =~ ^[Nn]$ ]];then
    prompt "Do you want to route all traffic through Wireguard [Y/n]?" TRAFFIC
    if [[ "${TRAFFIC}" =~ ^[Nn]$ ]];then
        WG_ALLOWED_IPS_DEFAULT="${WG_IPV4_NETWORK}.0/24,${WG_IPV6_NETWORK}::/64"
        prompt "Custom client routing - default [${WG_ALLOWED_IPS_DEFAULT}]:" WG_ALLOWED_IPS
        WG_ALLOWED_IPS=${WG_ALLOWED_IPS:-${WG_ALLOWED_IPS_DEFAULT}}
    else
        WG_ALLOWED_IPS="0.0.0.0/0,::/0"
    fi
else
    WG_ALLOWED_IPS="${WG_IPV4_NETWORK}.0/24,${WG_IPV6_NETWORK}::/64"
fi

printf $success "\nDefault Allowed-IPs [Can be configured per client]: ${WG_ALLOWED_IPS}\n"

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
        prompt "Generate new Wireguard keys [y/N]?" GENKEYS
    fi

    GENKEYS=${GENKEYS:-${GENKEYS_DEFAULT}}
    if [[ "${GENKEYS}" =~ ^[Yy]$ ]];then
        PRIVATE_KEY=$(wg genkey)
        store_key ${PRIVATE_KEY}
        printf $success "\nServer Keys Generated\n"
    else
        PRIVATE_KEY=$(sudo cat /etc/wireguard/privatekey)
        printf $success "\nExisting Server Keys Used\n"
    fi
fi

PUBLIC_KEY=$(sudo cat /etc/wireguard/publickey)

sudo sed \
    -e "s#__WG_IPV4_NETWORK#${WG_IPV4_NETWORK}#g" \
    -e "s#__WG_IPV6_NETWORK#${WG_IPV6_NETWORK}#g" \
    -e "s#__WG_IPV4#${WG_IPV4}#g" \
    -e "s#__WG_IPV6#${WG_IPV6}#g" \
    -e "s#__WG_SERVER_PUBLIC_KEY#${PUBLIC_KEY}#g" \
    -e "s/__WG_PORT/${WG_PORT}/g" \
    -e "s#__WG_DEFAULT_ALLOWED_IPS#${WG_ALLOWED_IPS}#g" \
    ${TEMPLATE_PATH}/wireguard-tools.conf.sh.tmpl > ${CONFIG_PATH}

touch ${WG_CONFIG_PATH}
NON_INTERFACE_CONFIG=$(sed '/\[Interface\]/,/^$/d' "${WG_CONFIG_PATH}")

ESCAPED=$(echo "${NON_INTERFACE_CONFIG}" | sed 's/[&/\]/\\&/g')
ESCAPED=$(echo "$ESCAPED" | sed ':a;N;$!ba;s/\n/__NEWLINE__/g')

sudo sed \
    -e "s#__WG_ADDRESS#"${WG_IPV4},${WG_IPV6}"#g" \
    -e "s/__PORT/${WG_PORT}/g" \
    -e "s#__PRIVATE_KEY#${1}#g" \
    -e "s/__NIC/${NIC}/g" \
    -e "s#__NON_INTERFACE_CONFIG#${ESCAPED}#g" \
    -e "s#__NEWLINE__#$(printf '\\n')#g" \
    ${TEMPLATE_PATH}/wg0.conf.tmpl > ${WG_CONFIG_PATH}

printf $info "\nWireguard config: \n"
sudo cat ${WG_CONFIG_PATH}

${SCRIPT_PATH}/system.sh restart
${SCRIPT_PATH}/system.sh enable

printf $success "\nWireguard configured. \n"
printf $success "Create clients using the 'scripts/client-setup.sh' script. \n\n"




