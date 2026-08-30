#!/bin/bash
set -euo pipefail
if [ "$EUID" -ne 0 ]; then echo "Run with sudo: sudo ./install.sh"; exit 1; fi

VERSION="$(tr -d '[:space:]' < VERSION)"
APP_ROOT=/opt/feednode
RELEASE="$APP_ROOT/releases/$VERSION"
STATE=/var/lib/feednode

apt update
apt install -y python3-venv python3-pip network-manager chromium xserver-xorg xinit openbox x11-xserver-utils avahi-daemon curl
systemctl enable --now NetworkManager
id feednode >/dev/null 2>&1 || useradd -m -s /bin/bash -G video,input,render feednode
mkdir -p "$APP_ROOT/releases" "$STATE"/{config,credentials,cache} /etc/feednode
rm -rf "$RELEASE"
mkdir -p "$RELEASE"
cp app.py requirements.txt config.default.json VERSION "$RELEASE"/
cp -r static templates scripts updater "$RELEASE"/
ln -sfn "$RELEASE" "$APP_ROOT/current"

if [ ! -d "$APP_ROOT/venv" ]; then
  python3 -m venv "$APP_ROOT/venv"
fi
"$APP_ROOT/venv/bin/pip" install --upgrade pip
"$APP_ROOT/venv/bin/pip" install -r "$RELEASE/requirements.txt"

[ -f "$STATE/config/config.json" ] || cp "$RELEASE/config.default.json" "$STATE/config/config.json"
chown -R feednode:feednode "$APP_ROOT" "$STATE" /home/feednode

install -m 755 "$RELEASE/scripts/feednode-network" /usr/local/bin/feednode-network
install -m 755 "$RELEASE/scripts/feednode-reset" /usr/local/bin/feednode-reset
install -m 755 "$RELEASE/scripts/firstboot-network" /usr/local/bin/feednode-firstboot-network
install -m 755 "$RELEASE/scripts/kiosk.sh" /usr/local/bin/feednode-kiosk
install -m 644 services/feednode.service /etc/systemd/system/feednode.service
install -m 644 services/feednode-network.service /etc/systemd/system/feednode-network.service
install -m 644 services/feednode-kiosk.service /etc/systemd/system/feednode-kiosk.service
install -m 644 services/feednode-updater.service /etc/systemd/system/feednode-updater.service
install -m 644 services/feednode-updater.timer /etc/systemd/system/feednode-updater.timer

cat >/etc/sudoers.d/feednode <<'SUDO'
feednode ALL=(root) NOPASSWD: /usr/local/bin/feednode-network, /usr/local/bin/feednode-reset, /opt/feednode/current/scripts/update.sh *
SUDO
chmod 440 /etc/sudoers.d/feednode

if [ ! -f /etc/feednode/feednode.env ]; then
cat >/etc/feednode/feednode.env <<'ENV'
# Distributor/developer configuration. End users do not edit this.
TWITCH_CLIENT_ID=
FEEDNODE_GITHUB_REPO=iamvinyl/FeedNode
ENV
chmod 640 /etc/feednode/feednode.env
chown root:feednode /etc/feednode/feednode.env
fi

systemctl daemon-reload
systemctl enable feednode.service feednode-network.service feednode-kiosk.service feednode-updater.timer
hostnamectl set-hostname feednode
systemctl enable avahi-daemon
/usr/local/bin/feednode-network ap || true

printf '\nFeedNode %s installed.\nSetup AP: FeedNode-Setup\nSetup URL: http://10.42.0.1:8787/setup\nSettings: http://feednode.local:8787/settings\n\nBefore Twitch login, set TWITCH_CLIENT_ID in /etc/feednode/feednode.env and restart feednode.service.\n' "$VERSION"
