#!/bin/bash

SCRIPT_PATH=$(realpath "$(dirname "${BASH_SOURCE[0]}")/")
source ${SCRIPT_PATH}/shared.inc.sh
source ${CONFIG_PATH}

# ==============================

echo
sudo wg
echo
prompt "Which client do you want to remove?" PUBLIC_KEY

sudo wg set wg0 peer ${PUBLIC_KEY} remove
sudo wg
echo