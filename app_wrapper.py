import asyncio
import subprocess

import app as base

app = base.app


async def _run_system_action(*args):
    await asyncio.sleep(0.25)
    subprocess.Popen(
        ["sudo", "/usr/bin/systemctl", *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


@app.post("/api/feed/clear")
async def clear_feed():
    base.feed.clear()
    # Restart only the HDMI renderer so it immediately reloads the empty feed.
    asyncio.create_task(_run_system_action("restart", "feednode-kiosk.service"))
    return {"ok": True}


@app.post("/api/system/reboot")
async def reboot_feednode():
    # Return the HTTP response before systemd begins the reboot.
    asyncio.create_task(_run_system_action("reboot"))
    return {"ok": True}
