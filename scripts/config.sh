#!/bin/bash

SCRIPT_PATH=$(realpath "$(dirname "${BASH_SOURCE[0]}")/")
source ${SCRIPT_PATH}/shared.inc.sh
source ${CONFIG_PATH}

#================================

sudo sed \
	-e "s#__WG_CIDR#${WG_CIDR}#g" \
    -e "s/__PORT/${WG_PORT}/g" \
    -e "s#__PRIVATE_KEY#${1}#g" \
    -e "s/__NIC/${NIC}/g" \
    -e "s#__NON_INTERFACE_CONFIG#${ESCAPED}#g" \
    -e "s#__NEWLINE__#$(printf '\\n')#g" \
    ${TEMPLATE_PATH}/wg0.conf.tmpl > ${WG_CONFIG_PATH}

printf $info "\nWireguard config: \n"
sudo cat ${WG_CONFIG_PATH}