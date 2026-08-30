#!/bin/bash
set -e
xset s off
xset -dpms
xset s noblank
openbox-session &
sleep 3
exec chromium --kiosk --noerrdialogs --disable-infobars --disable-session-crashed-bubble --disable-features=Translate --app=http://127.0.0.1:8787/
