import asyncio
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse

import app as base
from scripts.rumble_connector import RumbleConnector

app = base.app
UPDATER = Path(__file__).resolve().parent / "updater" / "updater.py"
PYTHON = Path("/opt/feednode/venv/bin/python")
FIRMWARE_LAUNCHER = Path("/usr/local/bin/feednode-firmware-update")
VERSION_FILE = Path(__file__).resolve().parent / "VERSION"
DIAG_DIR = base.STATE / "logs"
DIAG_LOG = DIAG_DIR / "feednode-diagnostics.txt"
DIAG_OLD = DIAG_DIR / "feednode-diagnostics.1.txt"
DIAG_MAX_BYTES = 2 * 1024 * 1024
UPDATE_CACHE_SECONDS = 300.0
DEFAULT_FEED_ITEMS = 100
MIN_FEED_ITEMS = 10
MAX_FEED_ITEMS = 250
_update_cache = {"data": None, "checked": 0.0}
_update_lock = asyncio.Lock()
_diag_task = None


def configured_feed_limit():
    try:value=int((base.load_config().get("system") or {}).get("max_feed_items",DEFAULT_FEED_ITEMS))
    except Exception:value=DEFAULT_FEED_ITEMS
    return max(MIN_FEED_ITEMS,min(MAX_FEED_ITEMS,value))


def configured_update_feed():
    try:value=str((base.load_config().get("system") or {}).get("update_feed","stable")).lower()
    except Exception:value="stable"
    return value if value in {"stable","beta"} else "stable"


def diagnostic_enabled():
    try:return bool((base.load_config().get("system") or {}).get("diagnostic_logging",False))
    except Exception:return False


def installed_version():
    try:return VERSION_FILE.read_text().strip()
    except Exception:return "unknown"


def _rotate_diagnostic_log():
    try:
        if DIAG_LOG.exists() and DIAG_LOG.stat().st_size >= DIAG_MAX_BYTES:
            DIAG_OLD.unlink(missing_ok=True)
            DIAG_LOG.replace(DIAG_OLD)
    except Exception:
        pass


def diagnostic_log(category, message):
    if not diagnostic_enabled():
        return
    try:
        DIAG_DIR.mkdir(parents=True,exist_ok=True)
        _rotate_diagnostic_log()
        stamp=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        clean=str(message).replace("\r"," ").replace("\n"," ")
        with DIAG_LOG.open("a",encoding="utf-8") as handle:
            handle.write(f"{stamp} [{category}] {clean}\n")
    except Exception:
        pass


def _network_snapshot():
    try:
        result=base.run("hostname","-I")
        return " ".join(result.stdout.split()) or "unavailable"
    except Exception:return "unavailable"


def _diagnostic_state():
    twitch=base.twitch_status()
    return {
        "connected":bool(twitch.get("connected")),
        "listening":bool(twitch.get("listening")),
        "session_id":str(base._eventsub_state.get("session_id") or ""),
        "last_error":str(twitch.get("last_error") or ""),
        "subscriptions":tuple(twitch.get("subscriptions") or []),
        "reauth_required":bool(twitch.get("reauth_required")),
    }


async def diagnostic_monitor():
    previous=None
    previously_enabled=False
    last_health=0.0
    while True:
        enabled=diagnostic_enabled()
        if enabled and not previously_enabled:
            diagnostic_log("SYSTEM",f"Diagnostic logging enabled · build {installed_version()} · IP {_network_snapshot()}")
        if enabled:
            state=_diagnostic_state()
            if previous is None:
                diagnostic_log("TWITCH",f"Initial state connected={state['connected']} listening={state['listening']} session={state['session_id'] or '-'} subscriptions={len(state['subscriptions'])} reauth_required={state['reauth_required']}")
            else:
                if state["connected"]!=previous["connected"]:
                    diagnostic_log("TWITCH",f"OAuth connection state changed: {previous['connected']} -> {state['connected']}")
                if state["listening"]!=previous["listening"]:
                    diagnostic_log("TWITCH",f"EventSub listening changed: {previous['listening']} -> {state['listening']}")
                if state["session_id"]!=previous["session_id"]:
                    diagnostic_log("TWITCH",f"EventSub session changed: {previous['session_id'] or '-'} -> {state['session_id'] or '-'}")
                if state["subscriptions"]!=previous["subscriptions"]:
                    diagnostic_log("TWITCH",f"Subscriptions changed: {len(previous['subscriptions'])} -> {len(state['subscriptions'])} · {', '.join(state['subscriptions']) or 'none'}")
                if state["last_error"]!=previous["last_error"] and state["last_error"]:
                    diagnostic_log("TWITCH ERROR",state["last_error"])
                if state["reauth_required"]!=previous["reauth_required"]:
                    diagnostic_log("TWITCH",f"Reauthorization required changed: {previous['reauth_required']} -> {state['reauth_required']}")
            previous=state
            now=time.monotonic()
            if now-last_health>=60:
                last_event=base._eventsub_state.get("last_event")
                age=(int(time.time())-int(last_event)) if last_event else None
                diagnostic_log("HEALTH",f"IP {_network_snapshot()} · connected={state['connected']} listening={state['listening']} subscriptions={len(state['subscriptions'])} last_event_age={age if age is not None else 'none'}s")
                last_health=now
        else:
            previous=None
        previously_enabled=enabled
        await asyncio.sleep(0.5)


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
async def start_wrapper_services():
    global _diag_task
    rumble.start()
    if _diag_task is None or _diag_task.done():
        _diag_task=asyncio.create_task(diagnostic_monitor())


@app.on_event("shutdown")
async def stop_wrapper_services():
    global _diag_task
    diagnostic_log("SYSTEM","FeedNode backend shutting down")
    await rumble.stop()
    if _diag_task and not _diag_task.done():
        _diag_task.cancel()
        try:await _diag_task
        except asyncio.CancelledError:pass


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
    rumble.disconnect();return {"ok":True}


@app.get("/api/diagnostics/status")
def diagnostic_status():
    size=0
    for path in (DIAG_OLD,DIAG_LOG):
        try:size+=path.stat().st_size
        except Exception:pass
    return {"ok":True,"enabled":diagnostic_enabled(),"bytes":size,"path":"feednode-diagnostics.txt"}


@app.get("/api/diagnostics/export")
def diagnostic_export():
    parts=[f"FeedNode Diagnostic Export\nBuild: {installed_version()}\nGenerated: {datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')}\nIP: {_network_snapshot()}\n\n"]
    state=_diagnostic_state()
    parts.append("Current Twitch State\n")
    parts.append(f"connected={state['connected']}\nlistening={state['listening']}\nsession_id={state['session_id'] or '-'}\nsubscriptions={', '.join(state['subscriptions']) or 'none'}\nlast_error={state['last_error'] or 'none'}\nreauth_required={state['reauth_required']}\n\n")
    for label,path in (("Previous rotated log",DIAG_OLD),("Current log",DIAG_LOG)):
        parts.append(f"===== {label} =====\n")
        try:parts.append(path.read_text(encoding="utf-8",errors="replace"))
        except Exception:parts.append("(no log data)\n")
        parts.append("\n")
    filename=f"feednode-diagnostics-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
    return PlainTextResponse("".join(parts),headers={"Content-Disposition":f'attachment; filename="{filename}"'})


@app.post("/api/diagnostics/clear")
def diagnostic_clear():
    DIAG_LOG.unlink(missing_ok=True);DIAG_OLD.unlink(missing_ok=True)
    diagnostic_log("SYSTEM","Diagnostic log cleared")
    return {"ok":True}


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
