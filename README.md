# FeedNode v0.3.9 — DIY Raspberry Pi Installer

FeedNode is a Raspberry Pi unified chat and activity-feed appliance built for a dedicated HDMI display. It combines streaming-platform chat, activity events, native HDMI rendering, and a lightweight web settings interface into one always-on device.

This guide is written for someone building a FeedNode from scratch.

Version 0.3.9 uses a native pygame/SDL2 renderer through Raspberry Pi KMSDRM. No Chromium kiosk, X11 desktop, browser window, or desktop environment is required for the HDMI output.

## What you need

### Recommended hardware

- Raspberry Pi Zero 2 W or faster
- 16 GB microSD minimum
- 32 GB+ microSD recommended
- Reliable 5V power supply appropriate for your Raspberry Pi
- HDMI cable/adapter for your Pi model
- HDMI display or monitor
- Wi-Fi network
- Another phone, tablet, or computer for first-time setup

FeedNode is currently targeted at the Raspberry Pi Zero 2 W. A Pi 3, Pi 4, or Pi 5 should have more than enough performance.

The original single-core Raspberry Pi Zero W may require a reduced feature profile, especially for animated emotes and image-heavy feeds.

## Recommended operating system

Use:

```text
Raspberry Pi OS Lite (64-bit)
```

A desktop image is not required.

When writing the SD card with Raspberry Pi Imager, it is helpful to preconfigure:

- hostname: `feednode`
- username and password
- SSH enabled
- your normal Wi-Fi credentials if you want the Pi online immediately

Preconfiguring Wi-Fi is optional. FeedNode can create its own setup access point if no usable Wi-Fi connection exists.

## 1. Boot the Raspberry Pi

Insert the SD card, connect HDMI, and power on the Pi.

If you enabled SSH in Raspberry Pi Imager, connect from another computer with:

```bash
ssh YOUR_PI_USERNAME@feednode.local
```

If `feednode.local` does not resolve yet, find the Pi's IP address from your router and connect directly:

```bash
ssh YOUR_PI_USERNAME@192.168.1.123
```

You can also complete the install directly from a keyboard and monitor attached to the Pi.

## 2. Update Raspberry Pi OS

From the Pi terminal:

```bash
sudo apt update
sudo apt full-upgrade -y
sudo reboot
```

Reconnect after the reboot.

## 3. Install Git

FeedNode's installer handles the application dependencies, but Git is needed to download the repository.

```bash
sudo apt update
sudo apt install -y git
```

Verify:

```bash
git --version
```

## 4. Clone FeedNode

Clone the repository into your home folder:

```bash
cd ~
git clone https://github.com/iamvinyl/FeedNode.git
cd FeedNode
```

Check the included build version:

```bash
cat VERSION
```

For this guide it should report:

```text
0.3.9
```

## 5. Install FeedNode

Make the installer executable and run it as root:

```bash
chmod +x install.sh
sudo ./install.sh
```

The installer sets up FeedNode, including the web backend, native HDMI renderer, Wi-Fi setup mode, firmware updater, and `feednode.local` network name.

When installation completes, reboot:

```bash
sudo reboot
```

## 6. First boot

FeedNode normally starts automatically after reboot.

If the Pi already has working Wi-Fi, open the settings page from another device:

```text
http://feednode.local:8787/settings
```

If `.local` name resolution does not work, get the Pi's IP address from the terminal:

```bash
hostname -I
```

Then open:

```text
http://<feednode-ip>:8787/settings
```

Example:

```text
http://192.168.1.175:8787/settings
```

## 7. Wi-Fi setup without preconfigured Wi-Fi

If FeedNode cannot find a usable saved Wi-Fi connection, it should expose this wireless network:

```text
FeedNode-Setup
```

Connect your phone, tablet, or computer to `FeedNode-Setup`.

Then open:

```text
http://10.42.0.1:8787/setup
```

Select your normal Wi-Fi network and enter its password.

FeedNode will stop the setup AP, return Wi-Fi to normal client mode, scan for your network, and connect. If the connection fails, `FeedNode-Setup` is restored so you can try again.

Once FeedNode joins your normal Wi-Fi, reconnect your phone/computer to your normal network and open:

```text
http://feednode.local:8787/settings
```

or use the FeedNode IP address.

## Manual Wi-Fi commands

If you are working from the Pi terminal, FeedNode provides these helpers.

Start setup AP mode:

```bash
sudo feednode-network ap
```

Connect directly to Wi-Fi:

```bash
sudo feednode-network connect "YOUR_SSID" "YOUR_WIFI_PASSWORD"
```

Clear saved Wi-Fi and return to setup mode:

```bash
sudo feednode-network clear
```

Show the active Wi-Fi connection:

```bash
nmcli -g GENERAL.CONNECTION device show wlan0
```

Show the current IP address:

```bash
hostname -I
```

## 8. Connect Twitch

Open:

```text
http://feednode.local:8787/settings
```

Go to the **Accounts** section and choose **Connect Twitch**.

FeedNode uses Twitch Device Code OAuth, so the settings page will provide a Twitch activation address and code.

Current requested Twitch permissions are:

```text
user:read:chat
user:write:chat
user:bot
channel:read:subscriptions
bits:read
moderator:read:followers
channel:read:redemptions
```

Twitch is currently the implemented live connector.

FeedNode can display Twitch chat and supported activity events with:

- usernames
- Twitch username colors
- avatars
- badges
- static emotes
- animated emotes
- activity cards
- optional viewer/follower/subscriber stats

## Future platform connectors

The v0.3.9 UI and config already include future connector slots for:

- Rumble chat + activity
- YouTube chat + activity
- Kick chat + activity

These are placeholders for future connector builds and are not yet equivalent to the Twitch integration.

## 9. Configure the HDMI layout

FeedNode supports six main layouts:

1. Portrait Combined
2. Portrait Split — Activity top / Chat bottom
3. Portrait Split — Chat top / Activity bottom
4. Landscape Combined
5. Landscape Split — Activity left / Chat right
6. Landscape Split — Chat left / Activity right

Orientation options are:

```text
portrait
landscape
portrait_flipped
```

Use the **Layout** section in Settings to change orientation, combined/split mode, ordering, and split ratio.

## 10. Customize appearance

The Settings page includes controls for:

- background color
- message panel color
- text and muted-text colors
- username accent
- message text size
- username size
- event text size
- avatar size
- top statistics-bar styling
- animated emote limit
- optional HDMI build footer

### Per-platform activity colors

Activity/event cards can have separate panel colors for:

- FeedNode / System
- Twitch
- Rumble
- YouTube
- Kick

This lets configuration/style-update events look different from Twitch events and leaves room for future platform-specific styling.

### Restore appearance defaults

The **Restore Colors & Text Sizes** control resets the appearance-related color and text-size values while leaving layout settings alone.

It does not reset orientation, combined/split mode, order, or split ratio.

## 11. HDMI setup and idle states

If FeedNode is waiting for first-time Wi-Fi setup, the HDMI display shows setup instructions instead of appearing dead.

If FeedNode is on the LAN but Twitch is not connected, the HDMI display shows an account/setup-required state and the settings address.

Once activity or chat arrives, the normal unified feed display takes over.

## 12. Web interface addresses

Settings:

```text
http://feednode.local:8787/settings
```

Direct IP:

```text
http://<feednode-ip>:8787/settings
```

Browser preview of the feed:

```text
http://feednode.local:8787/
```

Setup page while connected to `FeedNode-Setup`:

```text
http://10.42.0.1:8787/setup
```

## 13. Firmware updates

After the first install, normal updates can be performed from the FeedNode Settings page.

The updater checks the latest GitHub release, verifies the downloaded package, installs the new build, and returns FeedNode to the HDMI feed.

During an update the HDMI display shows the FeedNode `UPDATING` splash.

Normal updates preserve your FeedNode settings and connected accounts.

## Manual source update

If you prefer to update from the Pi terminal:

```bash
cd ~/FeedNode
git pull
sudo ./install.sh
sudo reboot
```

## Factory reset

Factory reset clears:

- saved FeedNode configuration
- streaming account credentials
- cache
- saved Wi-Fi client connections

It then restores the default config and returns the device to `FeedNode-Setup` mode.

After factory reset, connect to:

```text
FeedNode-Setup
```

and open:

```text
http://10.42.0.1:8787/setup
```

## Current build

```text
FeedNode v0.3.9
Unified Chat Feed
```
