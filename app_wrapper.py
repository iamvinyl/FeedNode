import asyncio
import json
import subprocess
from pathlib import Path

from fastapi.responses import JSONResponse

import app as base

app = base.app
UPDATER = Path(__file__).resolve().parent / "updater" / "updater.py"
PYTHON = Path("/opt/feednode/venv/bin/python")


async def _run_system_action(*args):
    await asyncio.sleep(0.25)
    subprocess.Popen(
        ["sudo", "/usr/bin/systemctl", *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _run_updater(action: str):
    result = subprocess.run(
        [str(PYTHON), str(UPDATER), action],
        text=True,
        capture_output=True,
        timeout=180,
    )
    raw = (result.stdout or result.stderr or "").strip()
    try:
        data = json.loads(raw.splitlines()[-1]) if raw else {}
    except Exception:
        data = {"ok": False, "error": raw or f"Updater exited with code {result.returncode}"}
    if result.returncode and data.get("ok") is not False:
        data = {"ok": False, "error": data.get("error") or raw or "Update check failed"}
    return data


def _start_detached_update():
    return subprocess.run(
        [
            "sudo", "/usr/bin/systemd-run",
            "--unit=feednode-firmware-update",
            "--collect",
            str(PYTHON), str(UPDATER), "install",
        ],
        text=True,
        capture_output=True,
        timeout=15,
    )


@app.post("/api/feed/clear")
async def clear_feed():
    base.feed.clear()
    asyncio.create_task(_run_system_action("restart", "feednode-kiosk.service"))
    return {"ok": True}


@app.post("/api/system/reboot")
async def reboot_feednode():
    asyncio.create_task(_run_system_action("reboot"))
    return {"ok": True}


@app.get("/api/update/check")
async def update_check():
    data = await asyncio.to_thread(_run_updater, "check")
    return JSONResponse(data, status_code=200 if data.get("ok") else 503)


@app.post("/api/update/install")
async def update_install():
    check = await asyncio.to_thread(_run_updater, "check")
    if not check.get("ok"):
        return JSONResponse(check, status_code=503)
    if not check.get("update_available"):
        return {"ok": True, "message": "FeedNode is already up to date", **check}

    result = await asyncio.to_thread(_start_detached_update)
    if result.returncode:
        return JSONResponse(
            {"ok": False, "error": (result.stderr or result.stdout or "Unable to start firmware updater").strip()},
            status_code=500,
        )
    return {
        "ok": True,
        "installing": True,
        "installed": check.get("installed"),
        "available": check.get("available"),
        "reboot_required": check.get("reboot_required", False),
    }
