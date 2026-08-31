#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="/opt/feednode"
STATE_ROOT="/var/lib/feednode"
RELEASES="$APP_ROOT/releases"
CURRENT="$APP_ROOT/current"

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

ln -sfn "$TARGET" "$CURRENT.new"
mv -Tf "$CURRENT.new" "$CURRENT"

systemctl restart feednode.service
sleep 4

if ! "$CURRENT/scripts/healthcheck.sh"; then
  echo "Health check failed; rolling back" >&2
  if [[ -n "$PREVIOUS" && -d "$PREVIOUS" ]]; then
    ln -sfn "$PREVIOUS" "$CURRENT.rollback"
    mv -Tf "$CURRENT.rollback" "$CURRENT"
    systemctl restart feednode.service
  fi
  exit 1
fi

echo "FeedNode updated to $VERSION"
