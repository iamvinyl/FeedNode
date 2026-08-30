#!/bin/bash
set -euo pipefail
if [ "$EUID" -ne 0 ]; then echo "Run with sudo: sudo ./install.sh"; exit 1; fi
apt update
apt install -y python3-venv python3-pip network-manager chromium xserver-xorg xinit openbox x11-xserver-utils avahi-daemon
systemctl enable --now NetworkManager
id feednode >/dev/null 2>&1 || useradd -m -s /bin/bash -G video,input,render feednode
mkdir -p /opt/feednode /var/lib/feednode/{config,credentials,cache} /etc/feednode
cp app.py requirements.txt config.default.json /opt/feednode/
cp -r static templates /opt/feednode/
python3 -m venv /opt/feednode/venv
/opt/feednode/venv/bin/pip install --upgrade pip
/opt/feednode/venv/bin/pip install -r /opt/feednode/requirements.txt
[ -f /var/lib/feednode/config/config.json ] || cp /opt/feednode/config.default.json /var/lib/feednode/config/config.json
chown -R feednode:feednode /opt/feednode /var/lib/feednode /home/feednode
install -m 755 scripts/feednode-network /usr/local/bin/feednode-network
install -m 755 scripts/feednode-reset /usr/local/bin/feednode-reset
install -m 755 scripts/firstboot-network /usr/local/bin/feednode-firstboot-network
install -m 755 scripts/kiosk.sh /usr/local/bin/feednode-kiosk
install -m 644 services/feednode.service /etc/systemd/system/feednode.service
install -m 644 services/feednode-network.service /etc/systemd/system/feednode-network.service
install -m 644 services/feednode-kiosk.service /etc/systemd/system/feednode-kiosk.service
cat >/etc/sudoers.d/feednode <<'SUDO'
feednode ALL=(root) NOPASSWD: /usr/local/bin/feednode-network, /usr/local/bin/feednode-reset
SUDO
chmod 440 /etc/sudoers.d/feednode
if [ ! -f /etc/feednode/feednode.env ]; then
cat >/etc/feednode/feednode.env <<'ENV'
# Distributor/developer configuration. End users do not edit this.
TWITCH_CLIENT_ID=
ENV
chmod 640 /etc/feednode/feednode.env
chown root:feednode /etc/feednode/feednode.env
fi
systemctl daemon-reload
systemctl enable feednode.service feednode-network.service feednode-kiosk.service
hostnamectl set-hostname feednode
systemctl enable avahi-daemon
/usr/local/bin/feednode-network ap || true
printf '\nFeedNode installed.\nSetup AP: FeedNode-Setup\nSetup URL: http://10.42.0.1:8787/setup\nSettings: http://feednode.local:8787/settings\n\nBefore Twitch login, set TWITCH_CLIENT_ID in /etc/feednode/feednode.env and restart feednode.service.\n'
