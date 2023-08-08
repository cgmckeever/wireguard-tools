# wireguard-tools

# Grab the repo

```
sudo apt -y update; \
sudo apt -y install git

sudo git clone https://github.com/cgmckeever/wireguard-tools.git

wireguard-tools/scripts/prereq.sh

```

# Configure

## Using an existing Private Key

```
echo; read -p "Enter private key: " PRIVATE_KEY; echo ${PRIVATE_KEY} | sudo tee /etc/wireguard/privatekey | wg pubkey | sudo tee /etc/wireguard/publickey
```

##

```
wireguard-tools/scripts/setup.sh
```

# Add Clients