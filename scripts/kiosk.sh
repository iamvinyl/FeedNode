#!/bin/bash
set -euo pipefail

# FeedNode v0.1.3: Cog talks directly to DRM/KMS. No Cage, X11, Openbox,
# compositor cursor, or desktop session is involved.
export HOME=/home/feednode
export XDG_CACHE_HOME=/var/lib/feednode/cache/cog
export COG_PLATFORM_NAME=drm
export COG_PLATFORM_DRM_MODE_MAX="1920x1080"
mkdir -p "$XDG_CACHE_HOME"

for _ in $(seq 1 30); do
  if curl --silent --fail http://127.0.0.1:8787/api/status >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

exec /usr/bin/cog \
  --platform=drm \
  --doc-viewer \
  --webprocess-failure=restart \
  --bg-color=#05060aff \
  http://127.0.0.1:8787/
