#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="/opt/feednode"
STATE_ROOT="/var/lib/feednode"
RELEASES="$APP_ROOT/releases"
CURRENT="$APP_ROOT/current"
VENV="$APP_ROOT/venv"

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 <release.tar.gz> <version>" >&2
  exit 2
fi

ARCHIVE="$1"
VERSION="$2"
TARGET="$RELEASES/$VERSION"
PREVIOUS=""

mkdir -p "$RELEASES" "$STATE_ROOT"
if [[ -L "$CURRENT" ]]; then
  PREVIOUS="$(readlink -f "$CURRENT" || true)"
fi

rm -rf "$TARGET"
mkdir -p "$TARGET"
tar -xzf "$ARCHIVE" -C "$TARGET" --strip-components=1

for required in VERSION app.py requirements.txt scripts/healthcheck.sh; do
  [[ -e "$TARGET/$required" ]] || { echo "Release missing $required" >&2; rm -rf "$TARGET"; exit 1; }
done

ln -sfn "$TARGET" "$CURRENT.new"
mv -Tf "$CURRENT.new" "$CURRENT"

rollback() {
  echo "Update failed; rolling back" >&2
  if [[ -n "$PREVIOUS" && -d "$PREVIOUS" ]]; then
    ln -sfn "$PREVIOUS" "$CURRENT.rollback"
    mv -Tf "$CURRENT.rollback" "$CURRENT"
    systemctl daemon-reload || true
    systemctl restart feednode.service || true
    systemctl restart feednode-kiosk.service || true
  fi
}
trap rollback ERR

"$VENV/bin/pip" install -r "$CURRENT/requirements.txt"

[[ -f "$CURRENT/scripts/feednode-network" ]] && install -m 755 "$CURRENT/scripts/feednode-network" /usr/local/bin/feednode-network
[[ -f "$CURRENT/scripts/feednode-reset" ]] && install -m 755 "$CURRENT/scripts/feednode-reset" /usr/local/bin/feednode-reset
[[ -f "$CURRENT/scripts/firstboot-network" ]] && install -m 755 "$CURRENT/scripts/firstboot-network" /usr/local/bin/feednode-firstboot-network
[[ -f "$CURRENT/scripts/kiosk.sh" ]] && install -m 755 "$CURRENT/scripts/kiosk.sh" /usr/local/bin/feednode-kiosk
[[ -f "$CURRENT/scripts/boot-splash.sh" ]] && install -m 755 "$CURRENT/scripts/boot-splash.sh" /usr/local/bin/feednode-boot-splash
[[ -f "$CURRENT/scripts/firmware-update.sh" ]] && install -m 755 "$CURRENT/scripts/firmware-update.sh" /usr/local/bin/feednode-firmware-update

cat >/etc/sudoers.d/feednode <<'SUDO'
feednode ALL=(root) NOPASSWD: /usr/local/bin/feednode-network, /usr/local/bin/feednode-reset, /usr/local/bin/feednode-firmware-update, /opt/feednode/current/scripts/update.sh *, /usr/bin/systemctl restart feednode-kiosk.service, /usr/bin/systemctl reboot
SUDO
chmod 440 /etc/sudoers.d/feednode
visudo -cf /etc/sudoers.d/feednode >/dev/null

for unit in feednode.service feednode-network.service feednode-splash.service feednode-kiosk.service feednode-updater.service feednode-updater.timer; do
  [[ -f "$CURRENT/services/$unit" ]] && install -m 644 "$CURRENT/services/$unit" "/etc/systemd/system/$unit"
done

chown -R feednode:feednode "$CURRENT" "$STATE_ROOT"
systemctl daemon-reload
systemctl restart feednode.service
sleep 4

if ! "$CURRENT/scripts/healthcheck.sh"; then
  false
fi

systemctl restart feednode-kiosk.service
systemctl restart feednode-updater.timer || true
trap - ERR

echo "FeedNode updated to $VERSION"
