import asyncio
import json
import subprocess
import time
from pathlib import Path

from fastapi.responses import JSONResponse

import app as base

app = base.app
UPDATER = Path(__file__).resolve().parent / "updater" / "updater.py"
PYTHON = Path("/opt/feednode/venv/bin/python")
UPDATE_CACHE_SECONDS = 300.0
_update_cache = {"data": None, "checked": 0.0}
_update_lock = asyncio.Lock()


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


async def _update_status(force=False):
    now = time.monotonic()
    cached = _update_cache.get("data")
    if not force and cached is not None and now - float(_update_cache.get("checked", 0.0)) < UPDATE_CACHE_SECONDS:
        return cached

    async with _update_lock:
        now = time.monotonic()
        cached = _update_cache.get("data")
        if not force and cached is not None and now - float(_update_cache.get("checked", 0.0)) < UPDATE_CACHE_SECONDS:
            return cached
        data = await asyncio.to_thread(_run_updater, "check")
        _update_cache["data"] = data
        _update_cache["checked"] = time.monotonic()
        return data


def _start_detached_update():
    cmd = [
        "sudo", "/usr/bin/systemd-run",
        "--unit=feednode-firmware-update",
        "--collect",
    ]
    # Copy these already-loaded variables into the transient service without
    # exposing the token value in the systemd-run command line.
    cmd.append("--setenv=FEEDNODE_GITHUB_REPO")
    cmd.append("--setenv=FEEDNODE_GITHUB_TOKEN")
    cmd.extend([str(PYTHON), str(UPDATER), "install"])
    return subprocess.run(cmd, text=True, capture_output=True, timeout=15)


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
async def update_check(force: bool = False):
    data = await _update_status(force=force)
    return JSONResponse(data, status_code=200 if data.get("ok") else 503)


@app.post("/api/update/install")
async def update_install():
    check = await _update_status(force=True)
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
