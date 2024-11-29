# wireguard-tools

Quick setup and configure for Wireguard on Ubuntu

# Grab the repo

```
sudo apt -y update; \
sudo apt -y install git; \
sudo git clone https://github.com/cgmckeever/wireguard-tools.git /opt/wireguard-tools; \
cd /opt/wireguard-tools
```

# Prereqs

```
scripts/prereq.sh
```

# Setup

```
scripts/setup.sh
```

# Client Configure 

## Add Clients

```
scripts/client-setup.sh
```

Client confs `clients/`

## Remove Client

```
scripts/client-remove.sh
```

## Show Client Details 

```
scripts/client-conf {CLIENT_ALIAS}
```

# Backup

```
scripts/backup.sh
```