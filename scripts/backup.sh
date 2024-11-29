#!/bin/bash

SCRIPT_PATH=$(realpath "$(dirname "${BASH_SOURCE[0]}")/")
source ${SCRIPT_PATH}/shared.inc.sh
source ${CONFIG_PATH}

#================================

BUNDLE_BACKUP="wireguard-tools.$(date +%s).zip"

sudo mkdir -p /tmp/wg-backup/clients
cd /tmp/wg-backup

sudo cp -r ${CLIENT_PATH}/* clients/
sudo cp /etc/wireguard/privatekey . 
sudo cp /etc/wireguard/publickey .
sudo cp /etc/wireguard/wg0.conf .
sudo cp ${CONFIG_PATH} .

zip -r "${BACKUP_PATH}/${BUNDLE_BACKUP}" *
cd ${BACKUP_PATH}
rm -rf /tmp/wg-backup