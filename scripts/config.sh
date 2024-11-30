#!/bin/bash

SCRIPT_PATH=$(realpath "$(dirname "${BASH_SOURCE[0]}")/")
source ${SCRIPT_PATH}/shared.inc.sh
source ${CONFIG_PATH}

#================================

echo $2
if [ -z "${2}" ]; then
	echo 3
	ESCAPED=$(echo "${2}" | sed 's/[&/\]/\\&/g' | tr '\n' '__NEWLINE__')
fi 
echo "1"
echo $ESCAPED

sudo sed \
	-e "s#__WG_CIDR#${WG_CIDR}#g" \
    -e "s/__PORT/${WG_PORT}/g" \
    -e "s#__PRIVATE_KEY#${1}#g" \
    -e "s/__NIC/${NIC}/g" \
    -e "s#__NON_INTERFACE_CONFIG#${ESCAPED}#g" \
    -e "s#__NEWLINE__#$(echo -e '\n')#g" \
    ${TEMPLATE_PATH}/wg0.conf.tmpl > ${WG_CONFIG_PATH}

printf $info "\nWireguard config: \n"
sudo cat ${WG_CONFIG_PATH}