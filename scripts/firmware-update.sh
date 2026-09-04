#!/usr/bin/env bash
set -euo pipefail

UNIT=feednode-firmware-update.service
PYTHON=/opt/feednode/venv/bin/python
UPDATER=/opt/feednode/current/updater/updater.py
ENV_FILE=/etc/feednode/feednode.env
ACTION="${1:-install}"

case "$ACTION" in
  install|rollback-stable) ;;
  *) echo "Unsupported firmware action: $ACTION" >&2; exit 2 ;;
esac

# Clear a stale failed transient unit from a previous attempt, then launch the
# updater outside the web service cgroup so restarting FeedNode cannot kill it.
/usr/bin/systemctl reset-failed "$UNIT" >/dev/null 2>&1 || true

exec /usr/bin/systemd-run \
  --unit=feednode-firmware-update \
  --collect \
  --property="EnvironmentFile=$ENV_FILE" \
  "$PYTHON" "$UPDATER" "$ACTION"
