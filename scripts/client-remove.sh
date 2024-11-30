#!/bin/bash

SCRIPT_PATH=$(realpath "$(dirname "${BASH_SOURCE[0]}")/")
source ${SCRIPT_PATH}/shared.inc.sh
source ${CONFIG_PATH}

# ==============================


WG=$(wg)

PEERS=()
IPS=()

while read -r LINE; do
    if [[ "${LINE}" =~ ^peer:\ (.*) ]];then
        PEERS+=("${BASH_REMATCH[1]}")
    elif [[ "$line" =~ ^allowed\ ips:\ (.*) ]];then
        IPS+=("${BASH_REMATCH[1]}")
    fi
done <<< "${WG}"

printf $info "\nPeer List:"
for i in "${!peers[@]}"; do
    printf $warn "$((i + 1)). ${PEERS[$i]} / ${IPS[$i]}"
done

prompt "Which client do you want to remove?" PEER

if [[ "${PEER}" -gt 0 && "${PEER}" -le "${#PEERS[@]}" ]];then
    sudo wg set wg0 peer "${PEERS[$((PEER - 1))]}" remove

    echo
	sudo wg
	echo
else
	printf $warn "\nInvalid selection. \n"
fi

