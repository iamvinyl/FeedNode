# Changelog

## 0.2.0
- Replaced the HDMI browser/compositor stack with a native pygame/SDL2 KMSDRM renderer.
- Added Twitch EventSub chat ingestion, native emote fragments, avatars, follows, subscriptions, gifts, resubs, Bits, raids, Channel Points, automatic rewards, and Power-ups.
- Kept the FastAPI web settings and remote feed preview while reducing local display overhead.
- Updated the installer and systemd services for Raspberry Pi OS Lite Bookworm and native HDMI startup.

## 0.1.0 - Initial foundation

- FeedNode Raspberry Pi Zero 2 W appliance foundation
- Portrait and landscape combined/split feed layouts
- Adjustable split ratio and panel ordering
- Style configuration with Google Fonts, colors, sizes, avatars and badges
- Wi-Fi setup AP and recovery mode
- Twitch Device Code OAuth foundation
- Account, cache, Wi-Fi and factory reset plumbing
- Chromium kiosk startup
- WebSocket live feed frontend
- GitHub release/update scaffolding
- Versioned release directories with rollback support
- Twice-daily GitHub update check timer
