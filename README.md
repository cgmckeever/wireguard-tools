# wireguard-tools

Quick setup and configure for Wireguard on Ubuntu

# Grab the repo

```
sudo apt -y update; \
sudo apt -y install git; \
sudo git clone https://github.com/cgmckeever/wireguard-tools.git

```

# Prereqs

```
wireguard-tools/scripts/prereq.sh
```

# Configure

## Using an existing Private Key

```
wireguard-tools/scripts/add-key.sh
```

## Run script

```
wireguard-tools/scripts/setup.sh
```

# Add Clients

```
wireguard-tools/scripts/client-setup.sh
```

Client list `~/authorized-keys.wireguard`

# Remove Client

```
wireguard-tools/scripts/client-remove.sh
```
