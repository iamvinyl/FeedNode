import asyncio, json, os, shutil, subprocess, time
from pathlib import Path
from typing import Any

import httpx
import websockets
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

for p in (CONFIG_DIR, CREDS_DIR, CACHE_DIR):
    p.mkdir(parents=True, exist_ok=True)
if not CONFIG_FILE.exists():
    shutil.copy(DEFAULT_FILE, CONFIG_FILE)


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
    dead = []
    for ws in clients:
        try:
            await ws.send_json(item)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


@app.get("/")
async def home():
    return RedirectResponse("/settings", status_code=307)


@app.get("/display", response_class=HTMLResponse)
async def display(request: Request):
    return templates.TemplateResponse("display.html", {"request": request, "config": load_config()})


@app.get("/settings", response_class=HTMLResponse)
async def settings(request: Request):
    return templates.TemplateResponse("settings.html", {"request": request, "config": load_config()})


@app.get("/setup", response_class=HTMLResponse)
async def setup(request: Request):
    return templates.TemplateResponse("setup.html", {"request": request, "networks": scan_wifi()})


@app.get("/api/config")
def get_config():
    return load_config()


@app.post("/api/config")
async def set_config(request: Request):
    data = await request.json()
    save_config(data)
    await publish({"kind": "system", "event": "style_update", "text": "Configuration updated"})
    return {"ok": True}


@app.get("/api/feed")
def get_feed():
    return feed


@app.post("/api/demo")
async def demo(request: Request):
    d = await request.json()
    await publish(d)
    return {"ok": True}


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        clients.discard(ws)


def scan_wifi():
    r = run("nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", "yes")
    out = []
    seen = set()
    if r.returncode:
        return out
    for line in r.stdout.splitlines():
        parts = line.split(":")
        if not parts or not parts[0] or parts[0] in seen:
            continue
        seen.add(parts[0])
        out.append({"ssid": parts[0], "signal": parts[1] if len(parts) > 1 else "", "security": parts[2] if len(parts) > 2 else ""})
    return out


@app.get("/api/wifi/scan")
def api_scan():
    return scan_wifi()


@app.post("/api/wifi/connect")
async def wifi_connect(ssid: str = Form(...), password: str = Form("")):
    args = ["sudo", "/usr/local/bin/feednode-network", "connect", ssid]
    if password:
        args.append(password)
    r = run(*args)
    if r.returncode:
        raise HTTPException(500, r.stderr or r.stdout)
    return RedirectResponse("/settings", status_code=303)


@app.post("/api/network/setup-mode")
def setup_mode():
    r = run("sudo", "/usr/local/bin/feednode-network", "ap")
    return JSONResponse({"ok": r.returncode == 0, "output": r.stdout + r.stderr})


# ---- Twitch OAuth + EventSub ----
TWITCH_TOKEN = CREDS_DIR / "twitch.json"
TWITCH_PENDING = CREDS_DIR / "twitch_device_pending.json"
TWITCH_SCOPES = "user:read:chat user:write:chat user:bot channel:read:subscriptions bits:read moderator:read:followers channel:read:redemptions"
REQUIRED_TWITCH_SCOPES = set(TWITCH_SCOPES.split())
EVENTSUB_URL = "wss://eventsub.wss.twitch.tv/ws?keepalive_timeout_seconds=30"
_eventsub_task: asyncio.Task | None = None
_eventsub_state = {
    "listening": False,
    "session_id": None,
    "last_event": None,
    "last_error": None,
    "subscriptions": [],
}
_profile_cache: dict[str, dict[str, Any]] = {}


def twitch_client_id():
    override = os.getenv("TWITCH_CLIENT_ID", "").strip()
    if override:
        return override
    return str(load_distributor_config().get("twitch_client_id", "")).strip()


def read_twitch_token():
    if not TWITCH_TOKEN.exists():
        return None
    try:
        return json.loads(TWITCH_TOKEN.read_text())
    except Exception:
        return None


def twitch_status():
    d = read_twitch_token()
    if not d:
        return {"connected": False, "listening": False, "reauth_required": False}
    granted = set(d.get("scope") or d.get("scopes") or [])
    missing = sorted(REQUIRED_TWITCH_SCOPES - granted) if granted else []
    return {
        "connected": True,
        "login": d.get("login"),
        "display_name": d.get("display_name"),
        "user_id": d.get("user_id"),
        "listening": bool(_eventsub_state["listening"]),
        "last_error": _eventsub_state["last_error"],
        "subscriptions": list(_eventsub_state["subscriptions"]),
        "last_event": _eventsub_state["last_event"],
        "reauth_required": "channel:read:redemptions" in missing,
        "missing_scopes": missing,
    }


async def twitch_profile(user_id: str, access_token: str):
    if not user_id:
        return {}
    hit = _profile_cache.get(user_id)
    if hit and time.time() - hit.get("cached_at", 0) < 21600:
        return hit
    headers = {"Authorization": f"Bearer {access_token}", "Client-Id": twitch_client_id()}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://api.twitch.tv/helix/users", params={"id": user_id}, headers=headers)
            if r.status_code == 200 and r.json().get("data"):
                u = r.json()["data"][0]
                u["cached_at"] = time.time()
                _profile_cache[user_id] = u
                return u
    except Exception:
        pass
    return {}


def eventsub_spec(sub_type: str, user_id: str):
    # Conditions differ by subscription type. FeedNode listens to the
    # authenticated broadcaster's channel.
    if sub_type in {"channel.chat.message", "channel.chat.notification"}:
        return "1", {"broadcaster_user_id": user_id, "user_id": user_id}
    if sub_type == "channel.follow":
        return "2", {"broadcaster_user_id": user_id, "moderator_user_id": user_id}
    if sub_type == "channel.raid":
        return "1", {"to_broadcaster_user_id": user_id}
    if sub_type == "channel.channel_points_automatic_reward_redemption.add":
        return "2", {"broadcaster_user_id": user_id}
    return "1", {"broadcaster_user_id": user_id}


async def create_eventsub_subscription(access_token: str, session_id: str, sub_type: str, user_id: str):
    version, condition = eventsub_spec(sub_type, user_id)
    body = {
        "type": sub_type,
        "version": version,
        "condition": condition,
        "transport": {"method": "websocket", "session_id": session_id},
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Client-Id": twitch_client_id(),
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post("https://api.twitch.tv/helix/eventsub/subscriptions", json=body, headers=headers)
    if r.status_code not in (202, 409):
        raise RuntimeError(f"{sub_type} subscription failed ({r.status_code}): {r.text}")
    return True