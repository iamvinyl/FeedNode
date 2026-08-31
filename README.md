# FeedNode v0.2.0 — Native Display Build

FeedNode is a Raspberry Pi unified chat/activity appliance. Version 0.2.0 replaces the HDMI web browser with a native pygame/SDL2 renderer using Raspberry Pi OS Bookworm's KMSDRM video path.

## Recommended base OS

- Raspberry Pi OS Lite (64-bit), Bookworm
- Raspberry Pi Zero 2 W or faster
- 16 GB minimum microSD; 32 GB+ recommended for comfortable cache/update headroom

## What runs

- FastAPI/Uvicorn backend on port 8787
- Web settings UI at `http://feednode.local:8787/settings`
- Twitch OAuth + EventSub ingestion
- Native HDMI renderer via pygame + SDL2 + KMSDRM
- No Chromium, Cog, Cage, X11, Openbox, or desktop environment

## Fresh install

```bash
cd ~/feednode_v0.2.0
chmod +x install.sh
sudo ./install.sh
sudo reboot
```

If FeedNode has no saved Wi-Fi connection, it exposes `FeedNode-Setup`. The setup page is `http://10.42.0.1:8787/setup` while connected to that AP.

## Twitch

Connect Twitch from Settings. Channel Points require the `channel:read:redemptions` permission, so reconnect Twitch after upgrading from an older authorization if Settings reports reauthorization is required.

## Native display

The HDMI process is `feednode-kiosk.service` for compatibility with earlier builds, but it now launches `/opt/feednode/current/display.py` directly. It renders the same layout/style configuration without a browser.

Useful diagnostics:

```bash
sudo systemctl status feednode.service --no-pager
sudo systemctl status feednode-kiosk.service --no-pager
sudo journalctl -u feednode-kiosk.service -n 100 --no-pager
```
