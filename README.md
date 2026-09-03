# FeedNode v0.3.9

FeedNode is a Raspberry Pi unified chat and activity-feed appliance built for a dedicated HDMI display. It combines streaming-platform chat, activity events, native display rendering, and a lightweight web-based configuration interface into a single always-on device.

Version 0.3.9 uses a native pygame/SDL2 renderer through Raspberry Pi KMSDRM. No browser, X11 desktop, Chromium kiosk, or desktop environment is required for HDMI output.

## Recommended hardware

- Raspberry Pi Zero 2 W or faster
- Raspberry Pi OS Lite 64-bit
- 16 GB minimum microSD
- 32 GB+ recommended for update/cache headroom
- HDMI display in portrait or landscape orientation

Pi Zero 2 W is the current target hardware. The original Pi Zero W may require a reduced feature profile because of its single-core CPU and lower available performance.

## Core services

FeedNode installs as a system appliance under `/opt/feednode` and stores persistent user data under `/var/lib/feednode`.

- `feednode.service` — FastAPI/Uvicorn backend on port `8787`
- `feednode-kiosk.service` — native pygame/SDL2/KMSDRM HDMI renderer
- `feednode-splash.service` — boot/update splash
- `feednode-network.service` — first-boot and setup-network fallback
- `feednode-updater.timer` / `feednode-updater.service` — firmware update support

Persistent data:

```text
/var/lib/feednode/
    config/
    credentials/
    cache/
```

Installed releases:

```text
/opt/feednode/
    current -> releases/<version>
    releases/<version>/
    venv/
```

## Web interface

Settings:

```text
http://feednode.local:8787/settings
```

Direct-IP access also works:

```text
http://<feednode-ip>:8787/settings
```

Browser display preview:

```text
http://feednode.local:8787/
```

The settings interface includes:

- layout and orientation controls
- top statistics-bar styling
- chat/feed styling
- per-platform activity/event panel colors
- Twitch account connection controls
- future connector placeholders for Rumble, YouTube, and Kick
- firmware status and web-based updates
- Wi-Fi reset, account reset, cache clear, reboot, and factory reset controls
- optional HDMI build footer
- restore-default appearance control for colors and text sizes without changing layout settings

## HDMI display modes

FeedNode supports:

1. Portrait Combined
2. Portrait Split — Activity top / Chat bottom
3. Portrait Split — Chat top / Activity bottom
4. Landscape Combined
5. Landscape Split — Activity left / Chat right
6. Landscape Split — Chat left / Activity right

Orientations:

- `portrait`
- `landscape`
- `portrait_flipped`

The native renderer supports Twitch avatars, badges, platform labels, username colors, static emotes, and animated Twitch emotes.

## Per-platform activity colors

Activity/event cards can use independent panel colors for:

- FeedNode / System events
- Twitch
- Rumble
- YouTube
- Kick

Existing configurations that only contain the older single `event_panel` value continue to work through compatibility fallback behavior.

## Empty / setup display states

FeedNode no longer presents an apparently dead black HDMI screen when the feed is empty after a reset.

When the device is in setup AP mode, HDMI displays setup instructions for `FeedNode-Setup`.

When FeedNode is on the LAN but Twitch is not connected, HDMI displays an account/setup-required state and the local settings address.

## Wi-Fi setup and recovery

If FeedNode has no usable saved Wi-Fi connection, it exposes:

```text
FeedNode-Setup
```

While connected to that AP, open:

```text
http://10.42.0.1:8787/setup
```

The network helper explicitly tears down the setup AP before joining a normal Wi-Fi network, switches `wlan0` back into client mode, rescans, and reconnects. If the client connection fails, FeedNode recreates the setup AP so the device is not stranded.

Factory/Wi-Fi reset recovery also restarts Avahi/mDNS, verifies the backend, and restarts the HDMI renderer.

Useful manual network commands:

```bash
sudo feednode-network ap
sudo feednode-network connect "SSID" "PASSWORD"
sudo feednode-network clear
```

## Twitch

Twitch is the currently implemented live platform connector.

FeedNode uses Twitch Device Code OAuth and EventSub for chat/activity ingestion.

Current requested scopes:

```text
user:read:chat
user:write:chat
user:bot
channel:read:subscriptions
bits:read
moderator:read:followers
channel:read:redemptions
```

Connect or reconnect Twitch from the Accounts section in Settings.

The native renderer can display:

- chat messages
- avatars
- username colors
- badges
- Twitch emotes
- animated emotes
- activity events
- optional viewer/follower/subscriber top-bar stats

## Future platform connectors

FeedNode currently reserves configuration/UI slots for:

- Rumble chat + activity
- YouTube chat + activity
- Kick chat + activity

These placeholders are intentionally present so future connectors can share the same unified feed and per-platform styling model.

## Firmware updates

FeedNode supports web-based firmware updates from the Settings page.

The updater:

1. checks the latest GitHub release
2. downloads the release package
3. verifies its SHA-256 digest
4. extracts the complete new release
5. executes the **new release's packaged `install.sh`**
6. verifies `/opt/feednode/current/VERSION`
7. returns to the HDMI feed

This uses the same installer path as a manual install so service definitions, helpers, dependencies, display code, and updater components are all upgraded together.

During firmware installation the HDMI display shows the FeedNode `UPDATING` splash.

## Fresh install

Clone or unpack the FeedNode repository, then run:

```bash
chmod +x install.sh
sudo ./install.sh
sudo reboot
```

The installer creates the release structure, virtual environment, systemd services, network helpers, splash/display services, and persistent state directories.

Existing configuration and credentials under `/var/lib/feednode` are preserved during normal firmware upgrades.

## Release workflow

`VERSION` is the authoritative build version.

Releases use tags in the form:

```text
vX.Y.Z
```

Example:

```bash
cd ~/Feednode
git pull
git tag -a v0.3.9 -m "FeedNode v0.3.9"
git push origin v0.3.9
```

The GitHub Actions release workflow packages the repository and generates the release archive, SHA-256 file, and update manifest.

## Diagnostics

Backend:

```bash
systemctl status feednode.service --no-pager
sudo journalctl -u feednode.service -n 100 --no-pager
curl -s http://127.0.0.1:8787/api/status
```

Native HDMI renderer:

```bash
systemctl status feednode-kiosk.service --no-pager
sudo journalctl -u feednode-kiosk.service -n 100 --no-pager -l
```

Network:

```bash
nmcli -g GENERAL.CONNECTION device show wlan0
hostname -I
systemctl status avahi-daemon --no-pager
```

Updater:

```bash
curl -s http://127.0.0.1:8787/api/update/check
curl -s 'http://127.0.0.1:8787/api/update/check?force=true'
systemctl status feednode-firmware-update.service --no-pager
sudo journalctl -u feednode-firmware-update.service -n 200 --no-pager
```

## Current build

```text
FeedNode v0.3.9
Unified Chat Feed
```
