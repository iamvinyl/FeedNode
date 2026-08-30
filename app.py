import asyncio, json, os, shutil, subprocess, time
from pathlib import Path
from typing import Any
import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).resolve().parent
STATE = Path(os.getenv("FEEDNODE_STATE", "/var/lib/feednode"))
CONFIG_DIR = STATE / "config"
CREDS_DIR = STATE / "credentials"
CACHE_DIR = STATE / "cache"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_FILE = BASE / "config.default.json"
DISTRIBUTOR_FILE = BASE / "config" / "distributor.json"

for p in (CONFIG_DIR, CREDS_DIR, CACHE_DIR): p.mkdir(parents=True, exist_ok=True)
if not CONFIG_FILE.exists(): shutil.copy(DEFAULT_FILE, CONFIG_FILE)

def load_config():
    return json.loads(CONFIG_FILE.read_text())

def save_config(data):
    CONFIG_FILE.write_text(json.dumps(data, indent=2))

def load_distributor_config():
    try:
        return json.loads(DISTRIBUTOR_FILE.read_text()) if DISTRIBUTOR_FILE.exists() else {}
    except Exception:
        return {}

def run(*args):
    return subprocess.run(args, text=True, capture_output=True)

app = FastAPI(title="FeedNode")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
clients: set[WebSocket] = set()
feed: list[dict[str, Any]] = []
MAX_ITEMS = 100

async def publish(item: dict[str, Any]):
    item.setdefault("ts", int(time.time()))
    feed.append(item)
    del feed[:-MAX_ITEMS]
    dead=[]
    for ws in clients:
        try: await ws.send_json(item)
        except Exception: dead.append(ws)
    for ws in dead: clients.discard(ws)

@app.get("/", response_class=HTMLResponse)
async def display(request: Request):
    return templates.TemplateResponse("display.html", {"request": request, "config": load_config()})

@app.get("/settings", response_class=HTMLResponse)
async def settings(request: Request):
    return templates.TemplateResponse("settings.html", {"request": request, "config": load_config()})

@app.get("/setup", response_class=HTMLResponse)
async def setup(request: Request):
    return templates.TemplateResponse("setup.html", {"request": request, "networks": scan_wifi()})

@app.get("/api/config")
def get_config(): return load_config()

@app.post("/api/config")
async def set_config(request: Request):
    data=await request.json(); save_config(data); await publish({"kind":"system","event":"style_update","text":"Configuration updated"}); return {"ok":True}

@app.get("/api/feed")
def get_feed(): return feed

@app.post("/api/demo")
async def demo(request: Request):
    d=await request.json(); await publish(d); return {"ok":True}

@app.websocket("/ws")
async def websocket(ws: WebSocket):
    await ws.accept(); clients.add(ws)
    try:
        while True: await ws.receive_text()
    except WebSocketDisconnect: clients.discard(ws)


def scan_wifi():
    r=run("nmcli","-t","-f","SSID,SIGNAL,SECURITY","dev","wifi","list","--rescan","yes")
    out=[]; seen=set()
    if r.returncode:
        return out
    for line in r.stdout.splitlines():
        parts=line.split(":")
        if not parts or not parts[0] or parts[0] in seen: continue
        seen.add(parts[0]); out.append({"ssid":parts[0],"signal":parts[1] if len(parts)>1 else "","security":parts[2] if len(parts)>2 else ""})
    return out

@app.get("/api/wifi/scan")
def api_scan(): return scan_wifi()

@app.post("/api/wifi/connect")
async def wifi_connect(ssid: str=Form(...), password: str=Form("")):
    args=["sudo","/usr/local/bin/feednode-network","connect",ssid]
    if password: args.append(password)
    r=run(*args)
    if r.returncode: raise HTTPException(500, r.stderr or r.stdout)
    return RedirectResponse("/settings", status_code=303)

@app.post("/api/network/setup-mode")
def setup_mode():
    r=run("sudo","/usr/local/bin/feednode-network","ap")
    return JSONResponse({"ok":r.returncode==0,"output":r.stdout+r.stderr})

@app.get("/api/status")
def status():
    wifi=run("nmcli","-t","-f","GENERAL.CONNECTION,IP4.ADDRESS","dev","show","wlan0")
    return {"device":"FeedNode","network":wifi.stdout.strip(),"twitch":twitch_status()}

TWITCH_TOKEN = CREDS_DIR / "twitch.json"

def twitch_client_id():
    override = os.getenv("TWITCH_CLIENT_ID", "").strip()
    if override:
        return override
    return str(load_distributor_config().get("twitch_client_id", "")).strip()

def twitch_status():
    if not TWITCH_TOKEN.exists(): return {"connected":False}
    try:
        d=json.loads(TWITCH_TOKEN.read_text())
        return {"connected":True,"login":d.get("login"),"display_name":d.get("display_name")}
    except: return {"connected":False}

@app.post("/api/twitch/device/start")
async def twitch_device_start():
    cid=twitch_client_id()
    if not cid: raise HTTPException(500,"Twitch Client ID is not configured in this FeedNode build")
    scopes="user:read:chat user:write:chat user:bot channel:read:subscriptions bits:read moderator:read:followers"
    async with httpx.AsyncClient(timeout=15) as c:
        r=await c.post("https://id.twitch.tv/oauth2/device", data={"client_id":cid,"scopes":scopes})
        r.raise_for_status(); d=r.json()
    (CREDS_DIR/"twitch_device_pending.json").write_text(json.dumps(d))
    return d

@app.post("/api/twitch/device/poll")
async def twitch_device_poll():
    cid=twitch_client_id(); p=CREDS_DIR/"twitch_device_pending.json"
    if not p.exists(): raise HTTPException(400,"No pending Twitch authorization")
    d=json.loads(p.read_text())
    async with httpx.AsyncClient(timeout=15) as c:
        r=await c.post("https://id.twitch.tv/oauth2/token", data={"client_id":cid,"device_code":d["device_code"],"grant_type":"urn:ietf:params:oauth:grant-type:device_code"})
        if r.status_code >= 400:
            return JSONResponse({"ok":False,"pending":True,"detail":r.text},status_code=202)
        tok=r.json()
        headers={"Authorization":f"OAuth {tok['access_token']}"}
        v=await c.get("https://id.twitch.tv/oauth2/validate",headers=headers); info=v.json() if v.status_code==200 else {}
    tok.update({"login":info.get("login"),"user_id":info.get("user_id")})
    TWITCH_TOKEN.write_text(json.dumps(tok,indent=2)); p.unlink(missing_ok=True)
    return {"ok":True,"login":tok.get("login")}

@app.post("/api/twitch/disconnect")
def twitch_disconnect():
    TWITCH_TOKEN.unlink(missing_ok=True); return {"ok":True}

@app.post("/api/reset/{level}")
def reset(level: str):
    if level not in {"wifi","accounts","cache","factory"}: raise HTTPException(400,"Unknown reset level")
    r=run("sudo","/usr/local/bin/feednode-reset",level)
    return JSONResponse({"ok":r.returncode==0,"output":r.stdout+r.stderr}, status_code=200 if r.returncode==0 else 500)
