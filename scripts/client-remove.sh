#!/bin/bash

SCRIPT_PATH=$(realpath "$(dirname "${BASH_SOURCE[0]}")/")
source ${SCRIPT_PATH}/shared.inc.sh
source ${CONFIG_PATH}

# ==============================


WG=$(wg)

ALIASES=()
PEERS=()
IPS=()

while read -r LINE; do
    if [[ "${LINE}" =~ ^peer:\ (.*) ]];then
    	PEER_KEY=${BASH_REMATCH[1]}
        PEERS+=(${PEER_KEY})

        for CLIENT in "${CLIENT_PATH}"/*.conf; do
		    ALIAS=$(basename "${CLIENT}" .conf)

		    PRIVATE_KEY=$(grep -oP 'PrivateKey = \K.+' "${CLIENT}")
		    PUBLIC_KEY=$(echo "${PRIVATE_KEY}" | wg pubkey)

		    if [[ "${PUBLIC_KEY}" == "${PEER_KEY}" ]]; then
		        ALIASES+=(${ALIAS})
		        break
		    fi
		done

    elif [[ "${LINE}" =~ ^[[:space:]]*allowed\ ips:\ (.*) ]];then
        IPS+=("${BASH_REMATCH[1]}")
    fi
done <<< "${WG}"

printf $info "\nPeer List:\n"
for i in "${!PEERS[@]}"; do
    printf $warn "$((i + 1)). ${ALIASES[$i]}: ${PEERS[$i]} / ${IPS[$i]}\n"
done

prompt "Which client do you want to remove?" PEER

if [[ "${PEER}" -gt 0 && "${PEER}" -le "${#PEERS[@]}" ]];then
    sudo wg set wg0 peer "${PEERS[$((PEER - 1))]}" remove
    printf $success "Peer "${PEERS[$((PEER - 1))]}" removed \n"

    echo
	sudo wg
	echo
else
	printf $warn "\nInvalid selection. \n"
fi

