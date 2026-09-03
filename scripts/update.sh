#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="/opt/feednode"
STATE_ROOT="/var/lib/feednode"
RELEASES="$APP_ROOT/releases"
CURRENT="$APP_ROOT/current"
VENV="$APP_ROOT/venv"
VENV_NEW="$APP_ROOT/venv.new"
VENV_OLD="$APP_ROOT/venv.old"
ENV_DIR="/etc/feednode"
ENV_FILE="$ENV_DIR/feednode.env"

if [[ $EUID -ne 0 ]]; then
  echo "FeedNode firmware update must run as root" >&2
  exit 1
fi

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 <release.tar.gz> <version>" >&2
  exit 2
fi

ARCHIVE="$1"
VERSION="$2"
TARGET="$RELEASES/$VERSION"
PREVIOUS=""

mkdir -p "$RELEASES" "$STATE_ROOT"/{config,credentials,cache} "$ENV_DIR"
if [[ -L "$CURRENT" ]]; then
  PREVIOUS="$(readlink -f "$CURRENT" || true)"
fi

# Build the target release completely before switching current.
rm -rf "$TARGET"
mkdir -p "$TARGET"
tar -xzf "$ARCHIVE" -C "$TARGET" --strip-components=1

for required in VERSION app.py app_wrapper.py display.py stats_display.py requirements.txt scripts/healthcheck.sh scripts/update.sh; do
  [[ -e "$TARGET/$required" ]] || {
    echo "Release missing $required" >&2
    rm -rf "$TARGET"
    exit 1
  }
done

PACKAGE_VERSION="$(tr -d '[:space:]' < "$TARGET/VERSION")"
if [[ "$PACKAGE_VERSION" != "$VERSION" ]]; then
  echo "Release VERSION ($PACKAGE_VERSION) does not match manifest version ($VERSION)" >&2
  rm -rf "$TARGET"
  exit 1
fi

rollback() {
  echo "Update failed; rolling back" >&2

  if [[ -d "$VENV_OLD" ]]; then
    rm -rf "$VENV" || true
    mv "$VENV_OLD" "$VENV" || true
  fi
  rm -rf "$VENV_NEW" || true

  if [[ -n "$PREVIOUS" && -d "$PREVIOUS" ]]; then
    ln -sfn "$PREVIOUS" "$CURRENT.rollback"
    mv -Tf "$CURRENT.rollback" "$CURRENT"
  fi

  systemctl daemon-reload || true
  systemctl restart feednode.service || true
  systemctl restart feednode-kiosk.service || true
}
trap rollback ERR

# Ensure the same runtime dependencies as a fresh install. apt install is
# idempotent and only changes packages that are missing/out of date.
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
  python3-venv python3-pip \
  network-manager avahi-daemon curl \
  python3-pygame libegl-dev fonts-dejavu-core libpam-systemd

systemctl enable --now NetworkManager
systemctl enable --now avahi-daemon

if id feednode >/dev/null 2>&1; then
  usermod -aG video,render,input feednode
else
  useradd -m -s /bin/bash -G video,render,input feednode
fi

# Build a fresh venv before touching the running one. This mirrors install.sh
# without leaving the device with a half-updated Python environment.
rm -rf "$VENV_NEW" "$VENV_OLD"
python3 -m venv --system-site-packages "$VENV_NEW"
"$VENV_NEW/bin/pip" install --upgrade pip
"$VENV_NEW/bin/pip" install -r "$TARGET/requirements.txt"

# Preserve user settings/credentials. Only seed config on devices that do not
# already have one.
if [[ ! -f "$STATE_ROOT/config/config.json" ]]; then
  cp "$TARGET/config.default.json" "$STATE_ROOT/config/config.json"
fi

# Refresh helpers from the new release before service restart.
[[ -f "$TARGET/scripts/feednode-network" ]] && install -m 755 "$TARGET/scripts/feednode-network" /usr/local/bin/feednode-network
[[ -f "$TARGET/scripts/feednode-reset" ]] && install -m 755 "$TARGET/scripts/feednode-reset" /usr/local/bin/feednode-reset
[[ -f "$TARGET/scripts/firstboot-network" ]] && install -m 755 "$TARGET/scripts/firstboot-network" /usr/local/bin/feednode-firstboot-network
[[ -f "$TARGET/scripts/kiosk.sh" ]] && install -m 755 "$TARGET/scripts/kiosk.sh" /usr/local/bin/feednode-kiosk
[[ -f "$TARGET/scripts/boot-splash.sh" ]] && install -m 755 "$TARGET/scripts/boot-splash.sh" /usr/local/bin/feednode-boot-splash
[[ -f "$TARGET/scripts/firmware-update.sh" ]] && install -m 755 "$TARGET/scripts/firmware-update.sh" /usr/local/bin/feednode-firmware-update

cat >/etc/sudoers.d/feednode <<'SUDO'
feednode ALL=(root) NOPASSWD: /usr/local/bin/feednode-network, /usr/local/bin/feednode-reset, /usr/local/bin/feednode-firmware-update, /opt/feednode/current/scripts/update.sh *, /usr/bin/systemctl restart feednode-kiosk.service, /usr/bin/systemctl reboot
SUDO
chmod 440 /etc/sudoers.d/feednode
visudo -cf /etc/sudoers.d/feednode >/dev/null

for unit in feednode.service feednode-network.service feednode-splash.service feednode-kiosk.service feednode-updater.service feednode-updater.timer; do
  [[ -f "$TARGET/services/$unit" ]] && install -m 644 "$TARGET/services/$unit" "/etc/systemd/system/$unit"
done

# Ensure updater environment exists without replacing an existing private token.
if [[ ! -f "$ENV_FILE" ]]; then
  cat >"$ENV_FILE" <<'ENV'
TWITCH_CLIENT_ID=
FEEDNODE_GITHUB_REPO=iamvinyl/FeedNode
FEEDNODE_GITHUB_TOKEN=
ENV
fi
grep -q '^FEEDNODE_GITHUB_REPO=' "$ENV_FILE" || echo 'FEEDNODE_GITHUB_REPO=iamvinyl/FeedNode' >> "$ENV_FILE"
grep -q '^FEEDNODE_GITHUB_TOKEN=' "$ENV_FILE" || echo 'FEEDNODE_GITHUB_TOKEN=' >> "$ENV_FILE"
chmod 640 "$ENV_FILE"
chown root:feednode "$ENV_FILE"

# Stop the backend immediately before switching code/venv. The updater itself
# is running in its own transient systemd unit and survives this restart.
systemctl stop feednode.service || true

if [[ -d "$VENV" ]]; then
  mv "$VENV" "$VENV_OLD"
fi
mv "$VENV_NEW" "$VENV"

ln -sfn "$TARGET" "$CURRENT.new"
mv -Tf "$CURRENT.new" "$CURRENT"

chown -R feednode:feednode "$TARGET" "$STATE_ROOT"

systemctl daemon-reload
systemctl enable feednode-network.service feednode-splash.service feednode-updater.timer >/dev/null 2>&1 || true
systemctl restart feednode.service

# Give Uvicorn time to bind, then verify that the new release is actually live.
sleep 4
if ! "$CURRENT/scripts/healthcheck.sh"; then
  false
fi

# The new backend passed healthcheck, so the old venv is no longer needed.
rm -rf "$VENV_OLD"

systemctl restart feednode-updater.timer || true
trap - ERR

echo "FeedNode updated to $VERSION"
