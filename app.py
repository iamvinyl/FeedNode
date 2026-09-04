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


def format_sub_tier(tier: Any, is_prime: bool = False):
    if is_prime:
        return "Prime"
    value = str(tier or "").strip()
    return {"1000": "Tier 1", "2000": "Tier 2", "3000": "Tier 3"}.get(value, f"Tier {value}" if value else "Subscription")


def eventsub_spec(sub_type: str, user_id: str):
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


async def handle_twitch_notification(message: dict[str, Any], access_token: str):
    meta = message.get("metadata", {})
    payload = message.get("payload", {})
    event = payload.get("event", {})
    sub_type = meta.get("subscription_type") or payload.get("subscription", {}).get("type")
    _eventsub_state["last_event"] = int(time.time())

    if sub_type == "channel.chat.message":
        uid = event.get("chatter_user_id", "")
        profile = await twitch_profile(uid, access_token)
        message = event.get("message") or {}
        await publish({
            "kind": "message",
            "platform": "Twitch",
            "user": event.get("chatter_user_name") or event.get("chatter_user_login") or "Twitch user",
            "text": message.get("text", ""),
            "fragments": message.get("fragments", []),
            "color": event.get("color") or "",
            "avatar": profile.get("profile_image_url", ""),
            "badges": event.get("badges", []),
            "message_id": event.get("message_id"),
        })
    elif sub_type == "channel.chat.notification":
        notice = event.get("notice_type", "activity")
        text = event.get("system_message") or (event.get("message") or {}).get("text", "")
        if notice == "sub":
            sub = event.get("sub") or {}
            text = f"subscribed · {format_sub_tier(sub.get('sub_tier'), bool(sub.get('is_prime')))}"
        elif notice == "resub":
            resub = event.get("resub") or {}
            tier = format_sub_tier(resub.get("sub_tier"), bool(resub.get("is_prime")))
            months = resub.get("cumulative_months") or resub.get("duration_months")
            text = f"resubscribed · {tier}" + (f" · {months} months" if months else "")
        await publish({
            "kind": "activity",
            "platform": "Twitch",
            "event": notice,
            "user": event.get("chatter_user_name") or event.get("chatter_user_login") or "Twitch",
            "text": text,
            "color": event.get("color") or "",
        })
    elif sub_type == "channel.follow":
        await publish({"kind":"activity","platform":"Twitch","event":"follow","user":event.get("user_name") or event.get("user_login") or "Viewer","text":"followed"})
    elif sub_type == "channel.subscribe":
        await publish({"kind":"activity","platform":"Twitch","event":"subscription","user":event.get("user_name") or event.get("user_login") or "Viewer","text":f"subscribed · {format_sub_tier(event.get('tier'))}"})
    elif sub_type == "channel.subscription.gift":
        who = "Anonymous" if event.get("is_anonymous") else (event.get("user_name") or event.get("user_login") or "Viewer")
        tier = format_sub_tier(event.get("tier"))
        await publish({"kind":"activity","platform":"Twitch","event":"gift_subs","user":who,"text":f"gifted {event.get('total',1)} sub(s) · {tier}"})
    elif sub_type == "channel.subscription.message":
        msg = (event.get("message") or {}).get("text", "")
        tier = format_sub_tier(event.get("tier"))
        months = event.get("cumulative_months", 1)
        await publish({"kind":"activity","platform":"Twitch","event":"resub","user":event.get("user_name") or event.get("user_login") or "Viewer","text":msg or f"resubscribed · {tier} · {months} months"})
    elif sub_type == "channel.cheer":
        who = "Anonymous" if event.get("is_anonymous") else (event.get("user_name") or event.get("user_login") or "Viewer")
        await publish({"kind":"activity","platform":"Twitch","event":"bits","user":who,"text":f"cheered {event.get('bits',0)} bits"})
    elif sub_type == "channel.raid":
        await publish({"kind":"activity","platform":"Twitch","event":"raid","user":event.get("from_broadcaster_user_name") or event.get("from_broadcaster_user_login") or "Raider","text":f"raided with {event.get('viewers',0)} viewers"})
    elif sub_type == "channel.channel_points_custom_reward_redemption.add":
        reward = event.get("reward") or {}
        title = reward.get("title") or "Channel Point Reward"
        extra = event.get("user_input") or ""
        text = f"redeemed {title}" + (f" · {extra}" if extra else "")
        await publish({"kind":"activity","platform":"Twitch","event":"channel_points","user":event.get("user_name") or event.get("user_login") or "Viewer","text":text})
    elif sub_type == "channel.channel_points_automatic_reward_redemption.add":
        reward = event.get("reward") or {}
        title = (reward.get("type") or "automatic reward").replace("_", " ")
        await publish({"kind":"activity","platform":"Twitch","event":"channel_points","user":event.get("user_name") or event.get("user_login") or "Viewer","text":f"redeemed {title}"})
    elif sub_type == "channel.custom_power_up_redemption.add":
        power = event.get("custom_power_up") or {}
        title = power.get("title") or "Power-up"
        bits = power.get("bits")
        text = f"used {title}" + (f" · {bits} bits" if bits is not None else "")
        await publish({"kind":"activity","platform":"Twitch","event":"power_up","user":event.get("user_name") or event.get("user_login") or "Viewer","text":text})


async def eventsub_worker():
    global _eventsub_task
    reconnect_url = None
    carry_subscriptions = False
    while True:
        tok = read_twitch_token()
        if not tok or not tok.get("access_token") or not tok.get("user_id"):
            _eventsub_state.update({"listening": False, "session_id": None, "subscriptions": []})
            await asyncio.sleep(5)
            continue

        access_token = tok["access_token"]
        user_id = str(tok["user_id"])
        url = reconnect_url or EVENTSUB_URL
        reconnect_url = None
        try:
            async with websockets.connect(url, open_timeout=15, close_timeout=5, ping_interval=None) as ws:
                welcome_raw = await asyncio.wait_for(ws.recv(), timeout=15)
                welcome = json.loads(welcome_raw)
                if welcome.get("metadata", {}).get("message_type") != "session_welcome":
                    raise RuntimeError("Twitch EventSub did not send a session_welcome message")
                session = welcome["payload"]["session"]
                session_id = session["id"]
                _eventsub_state["session_id"] = session_id
                _eventsub_state["last_error"] = None

                if not carry_subscriptions:
                    active = []
                    wanted = (
                        "channel.chat.message",
                        "channel.chat.notification",
                        "channel.follow",
                        "channel.subscribe",
                        "channel.subscription.gift",
                        "channel.subscription.message",
                        "channel.cheer",
                        "channel.raid",
                        "channel.channel_points_custom_reward_redemption.add",
                        "channel.channel_points_automatic_reward_redemption.add",
                        "channel.custom_power_up_redemption.add",
                    )
                    errors = []
                    for sub_type in wanted:
                        try:
                            await create_eventsub_subscription(access_token, session_id, sub_type, user_id)
                            active.append(sub_type)
                        except Exception as exc:
                            errors.append(str(exc))
                    _eventsub_state["subscriptions"] = active
                    _eventsub_state["last_error"] = " | ".join(errors) if errors else None
                carry_subscriptions = False
                _eventsub_state["listening"] = True

                async for raw in ws:
                    msg = json.loads(raw)
                    mtype = msg.get("metadata", {}).get("message_type")
                    if mtype == "notification":
                        await handle_twitch_notification(msg, access_token)
                    elif mtype == "session_reconnect":
                        reconnect_url = msg.get("payload", {}).get("session", {}).get("reconnect_url")
                        if reconnect_url:
                            carry_subscriptions = True
                            break
                    elif mtype == "revocation":
                        sub = msg.get("payload", {}).get("subscription", {})
                        _eventsub_state["last_error"] = f"Subscription revoked: {sub.get('type')} ({sub.get('status')})"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _eventsub_state["last_error"] = str(e)
        finally:
            _eventsub_state["listening"] = False
            _eventsub_state["session_id"] = None
            if not carry_subscriptions:
                _eventsub_state["subscriptions"] = []
        await asyncio.sleep(2)


def ensure_eventsub_worker():
    global _eventsub_task
    if _eventsub_task is None or _eventsub_task.done():
        _eventsub_task = asyncio.create_task(eventsub_worker())


@app.on_event("startup")
async def on_startup():
    ensure_eventsub_worker()


@app.on_event("shutdown")
async def on_shutdown():
    global _eventsub_task
    if _eventsub_task and not _eventsub_task.done():
        _eventsub_task.cancel()
        try:
            await _eventsub_task
        except asyncio.CancelledError:
            pass


@app.get("/api/status")
def status():
    wifi = run("nmcli", "-t", "-f", "GENERAL.CONNECTION,IP4.ADDRESS", "dev", "show", "wlan0")
    return {"device": "FeedNode", "network": wifi.stdout.strip(), "twitch": twitch_status()}


@app.post("/api/twitch/device/start")
async def twitch_device_start():
    cid = twitch_client_id()
    if not cid:
        raise HTTPException(500, "Twitch Client ID is not configured in this FeedNode build")
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post("https://id.twitch.tv/oauth2/device", data={"client_id": cid, "scopes": TWITCH_SCOPES})
        if r.status_code >= 400:
            raise HTTPException(r.status_code, f"Twitch device authorization failed: {r.text}")
        d = r.json()
    d["scopes"] = TWITCH_SCOPES
    TWITCH_PENDING.write_text(json.dumps(d))
    return d


@app.post("/api/twitch/device/poll")
async def twitch_device_poll():
    cid = twitch_client_id()
    if not TWITCH_PENDING.exists():
        raise HTTPException(400, "No pending Twitch authorization")
    d = json.loads(TWITCH_PENDING.read_text())
    scopes = d.get("scopes", TWITCH_SCOPES)
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post("https://id.twitch.tv/oauth2/token", data={
            "client_id": cid,
            "scopes": scopes,
            "device_code": d["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        })
        if r.status_code >= 400:
            try:
                err = r.json()
            except Exception:
                err = {"message": r.text}
            message = str(err.get("message", r.text))
            if message == "authorization_pending":
                return JSONResponse({"ok": False, "pending": True, "detail": message}, status_code=202)
            return JSONResponse({"ok": False, "pending": False, "detail": message}, status_code=r.status_code)
        tok = r.json()
        headers = {"Authorization": f"OAuth {tok['access_token']}"}
        v = await c.get("https://id.twitch.tv/oauth2/validate", headers=headers)
        info = v.json() if v.status_code == 200 else {}
    tok.update({
        "login": info.get("login"),
        "display_name": info.get("login"),
        "user_id": info.get("user_id"),
        "scopes": info.get("scopes", scopes.split()),
    })
    TWITCH_TOKEN.write_text(json.dumps(tok, indent=2))
    TWITCH_PENDING.unlink(missing_ok=True)
    _eventsub_state["last_error"] = None
    ensure_eventsub_worker()
    return {"ok": True, "login": tok.get("login")}


@app.post("/api/twitch/disconnect")
def twitch_disconnect():
    TWITCH_TOKEN.unlink(missing_ok=True)
    TWITCH_PENDING.unlink(missing_ok=True)
    _eventsub_state.update({"listening": False, "session_id": None, "subscriptions": [], "last_error": None})
    return {"ok": True}


@app.post("/api/reset/{level}")
def reset(level: str):
    if level not in {"wifi", "accounts", "cache", "factory"}:
        raise HTTPException(400, "Unknown reset level")
    r = run("sudo", "/usr/local/bin/feednode-reset", level)
    return JSONResponse({"ok": r.returncode == 0, "output": r.stdout + r.stderr}, status_code=200 if r.returncode == 0 else 500)
