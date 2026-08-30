# FeedNode
## Unified Chat Feed

FeedNode is a Raspberry Pi Zero 2 W kiosk appliance for a configurable combined or split live chat/activity display.

### v0.1.0 foundation
- Portrait combined feed
- Portrait split Activity/Chat or Chat/Activity
- Landscape combined feed
- Landscape split Activity/Chat or Chat/Activity
- Adjustable split ratio
- Style page with Google Font family/CSS URL, colors, sizes, avatars, platform labels and timestamps
- Wi-Fi setup AP (`FeedNode-Setup`) using NetworkManager
- Wi-Fi fallback/recovery
- Twitch Device Code OAuth endpoints (requires distributor-provided public Client ID)
- Account/cache/Wi-Fi/factory reset plumbing
- Chromium kiosk boot
- WebSocket live feed frontend
- Demo event API for connector testing
- GitHub release/update scaffolding

### Recommended image
Use current 64-bit Raspberry Pi OS Lite on a 32 GB high-endurance microSD.

### Install
Copy the `feednode` folder to the Pi, then:

```bash
cd feednode
sudo ./install.sh
sudo reboot
```

On first boot, connect a phone to **FeedNode-Setup**, then open:

`http://10.42.0.1:8787/setup`

After it joins Wi-Fi, settings are available at:

`http://feednode.local:8787/settings`

### Twitch developer setup
FeedNode uses Twitch's Device Code Grant so the end user does not enter OAuth tokens or client secrets. The distributor must register FeedNode as a Twitch application/public client and place the resulting public Client ID in:

`/etc/feednode/feednode.env`

Then:

```bash
sudo systemctl restart feednode
```

### Updates
FeedNode is designed to update from GitHub Releases. Stable releases contain a versioned tarball, SHA-256 checksum, and `manifest.json`; devices verify the checksum before installing and can roll back if the health check fails.

For tokenless end-user updates, the repository/releases must be publicly readable.

### Current connector status
The display/settings/network/reset foundation is implemented. Twitch Device OAuth is wired. Full Twitch EventSub ingestion, Twitch avatar/badge/emote resolution, Rumble ingestion, and YouTube ingestion are the next connector layer and are intentionally not faked in this v0.1.0 package.
