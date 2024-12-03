#!/bin/bash

SCRIPT_PATH=$(realpath "$(dirname "${BASH_SOURCE[0]}")/")
source ${SCRIPT_PATH}/shared.inc.sh
source ${CONFIG_PATH}

# =================

CLIENT_CONF=${CLIENT_PATH}/${1}.conf

printf $info "\nWireguard Client Conf \n"
printf $warn "   ${CLIENT_CONF}: \n"
cat ${CLIENT_CONF}

echo; echo
cat ${CLIENT_CONF} | qrencode -t ansiutf8