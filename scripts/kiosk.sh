#!/bin/bash
set -e

xset s off
xset -dpms
xset s noblank

openbox-session &
unclutter -idle 0 -root &

# Give the local FeedNode service a moment to become reachable.
for _ in $(seq 1 30); do
  if curl --silent --fail http://127.0.0.1:8787/api/status >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

exec chromium \
  --kiosk \
  --no-memcheck \
  --no-first-run \
  --no-default-browser-check \
  --noerrdialogs \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --disable-features=Translate \
  --hide-scrollbars \
  --app=http://127.0.0.1:8787/
