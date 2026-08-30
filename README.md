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
- Twitch Device Code OAuth endpoints using a distributor-provided public Client ID
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
FeedNode uses Twitch's **Device Code Grant** so end users can connect Twitch from a phone without manually creating or copying OAuth tokens.

#### Register the Twitch application
Create a Twitch Developer application for FeedNode and configure it as follows:

- **Name:** `FeedNode`
- **OAuth Redirect URL:** `http://localhost:3000`
- **Category:** `Other` or the closest general application/tool category available
- **Client Type:** `Public`

The redirect URL is required when registering the Twitch application, but FeedNode's Device Code flow does not depend on a browser redirect back to the Raspberry Pi.

#### Client ID
FeedNode requires the Twitch application's public **Client ID**. A Client Secret is not required for the Device Code public-client flow and should not be distributed with FeedNode.

The distributor Client ID is stored in:

`config/distributor.json`

Example:

```json
{
  "twitch_client_id": "YOUR_TWITCH_CLIENT_ID"
}
```

The value in `config/distributor.json` is included automatically in future FeedNode release packages, so end users do not need to enter a Client ID.

For development or testing, the built-in value can be overridden on a specific FeedNode by setting:

```bash
TWITCH_CLIENT_ID=your_test_client_id
```

in:

`/etc/feednode/feednode.env`

Then restart FeedNode:

```bash
sudo systemctl restart feednode
```

#### End-user Twitch login
Once the Client ID is configured by the distributor, the normal end-user flow is simply:

1. Open FeedNode Settings.
2. Select **Connect Twitch**.
3. FeedNode requests a Twitch Device Code.
4. The user opens the Twitch activation page on a phone or computer and enters the displayed code.
5. Twitch authorization tokens are stored locally on that FeedNode under `/var/lib/feednode/credentials/`.

End users should never need to enter the Twitch Client ID, Client Secret, access token, or refresh token manually.

### Updates
FeedNode is designed to update from GitHub Releases. Stable releases contain a versioned tarball, SHA-256 checksum, and `manifest.json`; devices verify the checksum before installing and can roll back if the health check fails.

For tokenless end-user updates, the repository/releases must be publicly readable.

### Current connector status
The display/settings/network/reset foundation is implemented. Twitch Device OAuth is wired. Full Twitch EventSub ingestion, Twitch avatar/badge/emote resolution, Rumble ingestion, and YouTube ingestion are the next connector layer and are intentionally not faked in this v0.1.0 package.
