#!/bin/bash

TEMPLATE_PATH="$(realpath ${SCRIPT_PATH}/../templates)"
CLIENT_PATH="$(realpath ${SCRIPT_PATH}/../clients)"
BACKUP_PATH="$(realpath ${SCRIPT_PATH}/../backups)"
CONFIG_PATH=${SCRIPT_PATH}/wireguard-tools.conf.sh

NIC=$(route | grep default | awk '{print $8}')

color() {
    STARTCOLOR="\e[$2";
    ENDCOLOR="\e[0m";
    export "$1"="$STARTCOLOR%b$ENDCOLOR"
}
color info 96m      # cyan
color success 92m   # green
color warning 93m   # yellow
color danger 91m    # red

store_key () {
    OUTPUT=$(echo ${1} | sudo tee /etc/wireguard/privatekey | wg pubkey | sudo tee /etc/wireguard/publickey)
}

pause () {
    echo
    if [[ ! -z $1 ]]; then
        msg $info "\nThis is a break pause; should be removed: $1\n\n"
    fi
    read -s -p "Press Enter to resume setup... "
    echo
}

prompt () {
    echo;
    read -p "${1} " ${2}
}