import asyncio
import json
import subprocess
import time
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

import app as base
from scripts.rumble_connector import RumbleConnector

app = base.app
UPDATER = Path(__file__).resolve().parent / "updater" / "updater.py"
PYTHON = Path("/opt/feednode/venv/bin/python")
FIRMWARE_LAUNCHER = Path("/usr/local/bin/feednode-firmware-update")
UPDATE_CACHE_SECONDS = 300.0
DEFAULT_FEED_ITEMS = 100
MIN_FEED_ITEMS = 10
MAX_FEED_ITEMS = 250
_update_cache = {"data": None, "checked": 0.0}
_update_lock = asyncio.Lock()


def configured_feed_limit():
    try:value=int((base.load_config().get("system") or {}).get("max_feed_items",DEFAULT_FEED_ITEMS))
    except Exception:value=DEFAULT_FEED_ITEMS
    return max(MIN_FEED_ITEMS,min(MAX_FEED_ITEMS,value))


def configured_update_feed():
    try:value=str((base.load_config().get("system") or {}).get("update_feed","stable")).lower()
    except Exception:value="stable"
    return value if value in {"stable","beta"} else "stable"


async def publish_limited(item):
    item.setdefault("ts",int(time.time()));base.feed.append(item);limit=configured_feed_limit()
    if len(base.feed)>limit:del base.feed[:-limit]
    dead=[]
    for ws in base.clients:
        try:await ws.send_json(item)
        except Exception:dead.append(ws)
    for ws in dead:base.clients.discard(ws)


base.publish=publish_limited
rumble=RumbleConnector(base.publish,base.CREDS_DIR/"rumble.json")


@app.on_event("startup")
async def start_rumble_connector():rumble.start()


@app.on_event("shutdown")
async def stop_rumble_connector():await rumble.stop()


@app.get("/api/rumble/status")
def rumble_status():return rumble.public_status()


@app.post("/api/rumble/connect")
async def rumble_connect(request:Request):
    try:
        payload=await request.json();status=await rumble.save_url(str(payload.get("api_url") or ""));config=base.load_config();config.setdefault("platforms",{}).setdefault("rumble",{})["enabled"]=True;base.save_config(config);return {"ok":True,**status}
    except Exception as exc:return JSONResponse({"ok":False,"error":str(exc)},status_code=400)


@app.post("/api/rumble/test")
async def rumble_test():
    url=rumble._read_url()
    if not url:return JSONResponse({"ok":False,"error":"No Rumble API credential is saved"},status_code=400)
    try:
        data=await rumble.test_url(url);rumble._update_metrics(data);rumble.state["connected"]=True;rumble.state["last_error"]=None;return {"ok":True,**rumble.public_status()}
    except Exception as exc:
        rumble.state["last_error"]=str(exc);return JSONResponse({"ok":False,"error":str(exc)},status_code=503)


@app.post("/api/rumble/disconnect")
def rumble_disconnect():
    rumble.disconnect();config=base.load_config();config.setdefault("platforms",{}).setdefault("rumble",{})["enabled"]=False;base.save_config(config);return {"ok":True}


async def _run_system_action(*args):
    await asyncio.sleep(0.25)
    subprocess.Popen(["sudo","/usr/bin/systemctl",*args],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)


def _run_updater(action:str):
    result=subprocess.run([str(PYTHON),str(UPDATER),action],text=True,capture_output=True,timeout=180);raw=(result.stdout or result.stderr or "").strip()
    try:data=json.loads(raw.splitlines()[-1]) if raw else {}
    except Exception:data={"ok":False,"error":raw or f"Updater exited with code {result.returncode}"}
    if result.returncode and data.get("ok") is not False:data={"ok":False,"error":data.get("error") or raw or "Update check failed"}
    return data


async def _update_status(force=False):
    now=time.monotonic();cached=_update_cache.get("data");channel=configured_update_feed();channel_changed=cached is not None and cached.get("channel")!=channel
    if not force and not channel_changed and cached is not None and now-float(_update_cache.get("checked",0.0))<UPDATE_CACHE_SECONDS:return cached
    async with _update_lock:
        now=time.monotonic();cached=_update_cache.get("data");channel=configured_update_feed();channel_changed=cached is not None and cached.get("channel")!=channel
        if not force and not channel_changed and cached is not None and now-float(_update_cache.get("checked",0.0))<UPDATE_CACHE_SECONDS:return cached
        data=await asyncio.to_thread(_run_updater,"check");_update_cache["data"]=data;_update_cache["checked"]=time.monotonic();return data


def _start_detached_update(action="install"):
    return subprocess.run(["sudo",str(FIRMWARE_LAUNCHER),action],text=True,capture_output=True,timeout=15)


@app.post("/api/feed/clear")
async def clear_feed():base.feed.clear();asyncio.create_task(_run_system_action("restart","feednode-kiosk.service"));return {"ok":True}


@app.post("/api/system/reboot")
async def reboot_feednode():asyncio.create_task(_run_system_action("reboot"));return {"ok":True}


@app.get("/api/update/check")
async def update_check(force:bool=False):
    data=await _update_status(force=force);return JSONResponse(data,status_code=200 if data.get("ok") else 503)


@app.post("/api/update/install")
async def update_install():
    check=await _update_status(force=True)
    if not check.get("ok"):return JSONResponse(check,status_code=503)
    if not check.get("update_available"):return {"ok":True,"message":"FeedNode is already up to date",**check}
    result=await asyncio.to_thread(_start_detached_update,"install")
    if result.returncode:return JSONResponse({"ok":False,"error":(result.stderr or result.stdout or "Unable to start firmware updater").strip()},status_code=500)
    return {"ok":True,"installing":True,"installed":check.get("installed"),"available":check.get("available"),"channel":check.get("channel",configured_update_feed()),"reboot_required":check.get("reboot_required",False)}


@app.post("/api/update/rollback-stable")
async def rollback_stable():
    check=await _update_status(force=True)
    if not check.get("ok"):return JSONResponse(check,status_code=503)
    stable=check.get("stable_available")
    if not stable:return JSONResponse({"ok":False,"error":"Unable to determine latest stable release"},status_code=503)
    if check.get("installed")==stable:return {"ok":True,"installing":False,"message":"Already on latest stable",**check}
    config=base.load_config();config.setdefault("system",{})["update_feed"]="stable";base.save_config(config);_update_cache["data"]=None;_update_cache["checked"]=0.0
    result=await asyncio.to_thread(_start_detached_update,"rollback-stable")
    if result.returncode:return JSONResponse({"ok":False,"error":(result.stderr or result.stdout or "Unable to start stable rollback").strip()},status_code=500)
    return {"ok":True,"installing":True,"installed":check.get("installed"),"available":stable,"channel":"stable","rollback":True}
