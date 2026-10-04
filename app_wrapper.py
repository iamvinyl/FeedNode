import asyncio
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import websockets
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
TWITCH_VALIDATE_SECONDS = 50 * 60
TWITCH_RECONNECT_MIN_SECONDS = 2
TWITCH_RECONNECT_MAX_SECONDS = 30
TWITCH_KEEPALIVE_GRACE_SECONDS = 5

TWITCH_EVENT_TYPES = (
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

_update_cache = {"data": None, "checked": 0.0}
_update_lock = asyncio.Lock()
_diag_task = None
_twitch_token_task = None
_twitch_token_lock = asyncio.Lock()
_twitch_last_validation = 0.0
_twitch_auth_state = {
    "state": "disconnected",
    "last_validation": None,
    "last_refresh": None,
    "refresh_count": 0,
    "last_error": None,
}


def configured_feed_limit():
    try:
        value = int((base.load_config().get("system") or {}).get("max_feed_items", DEFAULT_FEED_ITEMS))
    except Exception:
        value = DEFAULT_FEED_ITEMS
    return max(MIN_FEED_ITEMS, min(MAX_FEED_ITEMS, value))


def configured_update_feed():
    try:
        value = str((base.load_config().get("system") or {}).get("update_feed", "stable")).lower()
    except Exception:
        value = "stable"
    return value if value in {"stable", "beta"} else "stable"


def diagnostic_enabled():
    try:
        return bool((base.load_config().get("system") or {}).get("diagnostic_logging", False))
    except Exception:
        return False


def installed_version():
    try:
        return VERSION_FILE.read_text().strip()
    except Exception:
        return "unknown"


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
        DIAG_DIR.mkdir(parents=True, exist_ok=True)
        _rotate_diagnostic_log()
        stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        clean = str(message).replace("\r", " ").replace("\n", " ")
        with DIAG_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} [{category}] {clean}\n")
    except Exception:
        pass


def _network_snapshot():
    try:
        result = base.run("hostname", "-I")
        return " ".join(result.stdout.split()) or "unavailable"
    except Exception:
        return "unavailable"


def _safe_twitch_error(response):
    try:
        payload = response.json()
        return str(payload.get("message") or payload.get("error") or f"HTTP {response.status_code}")
    except Exception:
        return f"HTTP {response.status_code}"


def _atomic_write_twitch_token(token):
    temp = base.TWITCH_TOKEN.with_suffix(".tmp")
    temp.write_text(json.dumps(token, indent=2))
    try:
        temp.chmod(0o600)
    except Exception:
        pass
    os.replace(temp, base.TWITCH_TOKEN)


def _set_twitch_auth_state(state, error=None):
    _twitch_auth_state["state"] = state
    _twitch_auth_state["last_error"] = str(error) if error else None


async def _validate_twitch_access_token(access_token):
    if not access_token:
        return False, {}
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.get(
                "https://id.twitch.tv/oauth2/validate",
                headers={"Authorization": f"OAuth {access_token}"},
            )
    except Exception as exc:
        raise RuntimeError(f"Twitch token validation network error: {exc}") from exc
    if response.status_code == 200:
        return True, response.json()
    if response.status_code == 401:
        return False, {}
    raise RuntimeError(f"Twitch token validation failed: {_safe_twitch_error(response)}")


async def refresh_twitch_token(expected_access_token=None, reason="token invalid"):
    global _twitch_last_validation
    async with _twitch_token_lock:
        current = base.read_twitch_token() or {}
        access_token = str(current.get("access_token") or "")
        if expected_access_token and access_token and access_token != expected_access_token:
            diagnostic_log("TWITCH AUTH", "Refresh skipped because another task already rotated the access token")
            return current

        refresh_token = str(current.get("refresh_token") or "")
        if not refresh_token:
            _set_twitch_auth_state("reauth_required", "No Twitch refresh token is available")
            diagnostic_log("TWITCH AUTH", "Refresh unavailable: saved authorization has no refresh token")
            return None

        _set_twitch_auth_state("refreshing")
        diagnostic_log("TWITCH AUTH", f"Refreshing access token · reason={reason}")

        payload = {
            "client_id": base.twitch_client_id(),
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        distributor = base.load_distributor_config()
        client_secret = str(
            os.getenv("TWITCH_CLIENT_SECRET") or distributor.get("twitch_client_secret") or ""
        ).strip()
        if client_secret:
            payload["client_secret"] = client_secret

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post("https://id.twitch.tv/oauth2/token", data=payload)
        except Exception as exc:
            _set_twitch_auth_state("connected", f"Refresh network error: {exc}")
            diagnostic_log("TWITCH AUTH ERROR", f"Refresh network error: {exc}")
            return current or None

        if response.status_code != 200:
            message = _safe_twitch_error(response)
            _set_twitch_auth_state("reauth_required", message)
            base._eventsub_state["last_error"] = f"Twitch authorization expired: {message}"
            diagnostic_log(
                "TWITCH AUTH ERROR",
                f"Refresh failed · status={response.status_code} · {message}",
            )
            return None

        refreshed = dict(current)
        body = response.json()
        refreshed.update(body)
        if "scope" in body:
            refreshed["scopes"] = body.get("scope") or []

        valid, info = await _validate_twitch_access_token(str(refreshed.get("access_token") or ""))
        if not valid:
            _set_twitch_auth_state("reauth_required", "Twitch rejected the newly refreshed access token")
            diagnostic_log("TWITCH AUTH ERROR", "Twitch rejected the newly refreshed access token")
            return None

        refreshed["login"] = info.get("login") or refreshed.get("login")
        refreshed["display_name"] = info.get("login") or refreshed.get("display_name")
        refreshed["user_id"] = info.get("user_id") or refreshed.get("user_id")
        refreshed["scopes"] = (
            info.get("scopes")
            or refreshed.get("scopes")
            or refreshed.get("scope")
            or []
        )
        _atomic_write_twitch_token(refreshed)

        _twitch_last_validation = time.monotonic()
        stamp = int(time.time())
        _twitch_auth_state["last_validation"] = stamp
        _twitch_auth_state["last_refresh"] = stamp
        _twitch_auth_state["refresh_count"] = int(_twitch_auth_state.get("refresh_count") or 0) + 1
        _set_twitch_auth_state("connected")
        diagnostic_log(
            "TWITCH AUTH",
            f"Access token refreshed successfully · refresh_count={_twitch_auth_state['refresh_count']}",
        )
        return refreshed


async def ensure_valid_twitch_token(force=False):
    global _twitch_last_validation
    token = base.read_twitch_token()
    if not token or not token.get("access_token") or not token.get("user_id"):
        _set_twitch_auth_state("disconnected")
        return None

    now = time.monotonic()
    if (
        not force
        and _twitch_last_validation
        and now - _twitch_last_validation < TWITCH_VALIDATE_SECONDS
    ):
        return token

    access_token = str(token.get("access_token") or "")
    _set_twitch_auth_state("validating")
    diagnostic_log("TWITCH AUTH", "Validating saved access token")
    try:
        valid, _ = await _validate_twitch_access_token(access_token)
    except Exception as exc:
        _set_twitch_auth_state("connected", exc)
        diagnostic_log("TWITCH AUTH ERROR", str(exc))
        return token

    if valid:
        _twitch_last_validation = now
        _twitch_auth_state["last_validation"] = int(time.time())
        _set_twitch_auth_state("connected")
        diagnostic_log("TWITCH AUTH", "Access token validation succeeded")
        return token

    diagnostic_log("TWITCH AUTH", "Access token is invalid; attempting automatic refresh")
    return await refresh_twitch_token(
        expected_access_token=access_token,
        reason="validate returned 401",
    )


async def twitch_token_maintenance():
    while True:
        try:
            await ensure_valid_twitch_token(force=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            diagnostic_log(
                "TWITCH AUTH ERROR",
                f"Token maintenance exception: {type(exc).__name__}: {exc}",
            )
        await asyncio.sleep(60)


async def _create_eventsub_subscription(access_token, session_id, sub_type, user_id):
    version, condition = base.eventsub_spec(sub_type, user_id)
    body = {
        "type": sub_type,
        "version": version,
        "condition": condition,
        "transport": {"method": "websocket", "session_id": session_id},
    }

    async def send(token):
        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Id": base.twitch_client_id(),
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            return await client.post(
                "https://api.twitch.tv/helix/eventsub/subscriptions",
                json=body,
                headers=headers,
            )

    response = await send(access_token)
    if response.status_code == 401:
        diagnostic_log("TWITCH AUTH", f"401 creating {sub_type}; refreshing token")
        refreshed = await refresh_twitch_token(
            expected_access_token=access_token,
            reason=f"401 creating EventSub subscription {sub_type}",
        )
        if not refreshed:
            raise RuntimeError(
                f"{sub_type} subscription failed because Twitch authorization could not be refreshed"
            )
        access_token = str(refreshed.get("access_token") or "")
        response = await send(access_token)

    if response.status_code not in (202, 409):
        raise RuntimeError(
            f"{sub_type} subscription failed ({response.status_code}): {_safe_twitch_error(response)}"
        )
    return access_token


async def _open_eventsub_socket(url):
    diagnostic_log("TWITCH EVENTSUB", "Opening WebSocket connection")
    ws = await websockets.connect(
        url,
        open_timeout=15,
        close_timeout=5,
        ping_interval=None,
    )
    try:
        welcome_raw = await asyncio.wait_for(ws.recv(), timeout=15)
        welcome = json.loads(welcome_raw)
        if welcome.get("metadata", {}).get("message_type") != "session_welcome":
            raise RuntimeError("Twitch EventSub did not send a session_welcome message")
        session = welcome.get("payload", {}).get("session", {})
        if not session.get("id"):
            raise RuntimeError("Twitch EventSub welcome did not include a session ID")
        diagnostic_log(
            "TWITCH EVENTSUB",
            f"Welcome received · session={session.get('id')} · "
            f"keepalive={session.get('keepalive_timeout_seconds')}",
        )
        return ws, session
    except Exception:
        await ws.close()
        raise


async def hardened_eventsub_worker():
    reconnect_delay = TWITCH_RECONNECT_MIN_SECONDS
    while True:
        token = await ensure_valid_twitch_token(force=not bool(_twitch_last_validation))
        if not token:
            base._eventsub_state.update(
                {"listening": False, "session_id": None, "subscriptions": []}
            )
            await asyncio.sleep(5)
            continue

        access_token = str(token.get("access_token") or "")
        user_id = str(token.get("user_id") or "")
        ws = None
        connected_once = False
        try:
            _set_twitch_auth_state("connecting")
            ws, session = await _open_eventsub_socket(base.EVENTSUB_URL)
            session_id = str(session["id"])
            base._eventsub_state["session_id"] = session_id
            base._eventsub_state["last_error"] = None

            active = []
            errors = []
            for sub_type in TWITCH_EVENT_TYPES:
                try:
                    access_token = await _create_eventsub_subscription(
                        access_token,
                        session_id,
                        sub_type,
                        user_id,
                    )
                    active.append(sub_type)
                    diagnostic_log("TWITCH EVENTSUB", f"Subscribed · {sub_type}")
                except Exception as exc:
                    errors.append(str(exc))
                    diagnostic_log("TWITCH EVENTSUB ERROR", str(exc))

            if not active:
                raise RuntimeError("No Twitch EventSub subscriptions could be created")

            base._eventsub_state["subscriptions"] = active
            base._eventsub_state["last_error"] = " | ".join(errors) if errors else None
            base._eventsub_state["listening"] = True
            _set_twitch_auth_state("connected")
            connected_once = True
            reconnect_delay = TWITCH_RECONNECT_MIN_SECONDS
            diagnostic_log(
                "TWITCH EVENTSUB",
                f"Listening · session={session_id} · subscriptions={len(active)}",
            )

            keepalive = int(session.get("keepalive_timeout_seconds") or 30)
            timeout = max(10, keepalive + TWITCH_KEEPALIVE_GRACE_SECONDS)

            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except asyncio.TimeoutError as exc:
                    diagnostic_log(
                        "TWITCH EVENTSUB ERROR",
                        f"Keepalive timeout · no message received for {timeout}s · "
                        f"session={base._eventsub_state.get('session_id') or '-'}",
                    )
                    raise RuntimeError("Twitch EventSub keepalive timeout") from exc

                message = json.loads(raw)
                message_type = message.get("metadata", {}).get("message_type")

                if message_type == "notification":
                    live_token = base.read_twitch_token() or {}
                    await base.handle_twitch_notification(
                        message,
                        str(live_token.get("access_token") or access_token),
                    )
                elif message_type == "session_keepalive":
                    continue
                elif message_type == "session_reconnect":
                    reconnect_url = (
                        message.get("payload", {})
                        .get("session", {})
                        .get("reconnect_url")
                    )
                    if not reconnect_url:
                        raise RuntimeError(
                            "Twitch requested EventSub reconnect without a reconnect URL"
                        )

                    old_session = str(base._eventsub_state.get("session_id") or "")
                    _set_twitch_auth_state("reconnecting")
                    diagnostic_log(
                        "TWITCH EVENTSUB",
                        f"Twitch requested reconnect · old_session={old_session}",
                    )

                    try:
                        new_ws, new_session = await _open_eventsub_socket(reconnect_url)
                    except Exception as exc:
                        diagnostic_log(
                            "TWITCH EVENTSUB ERROR",
                            f"Reconnect handoff failed; falling back to fresh session · "
                            f"{type(exc).__name__}: {exc}",
                        )
                        raise

                    old_ws = ws
                    ws = new_ws
                    session = new_session
                    new_session_id = str(new_session["id"])
                    base._eventsub_state["session_id"] = new_session_id
                    base._eventsub_state["listening"] = True
                    keepalive = int(new_session.get("keepalive_timeout_seconds") or 30)
                    timeout = max(
                        10,
                        keepalive + TWITCH_KEEPALIVE_GRACE_SECONDS,
                    )
                    _set_twitch_auth_state("connected")
                    diagnostic_log(
                        "TWITCH EVENTSUB",
                        f"Reconnect handoff complete · {old_session} -> {new_session_id} · "
                        "subscriptions carried by Twitch",
                    )
                    try:
                        await old_ws.close()
                    except Exception:
                        pass
                elif message_type == "revocation":
                    subscription = message.get("payload", {}).get("subscription", {})
                    sub_type = subscription.get("type")
                    sub_status = subscription.get("status")
                    error = f"Subscription revoked: {sub_type} ({sub_status})"
                    base._eventsub_state["last_error"] = error
                    diagnostic_log("TWITCH EVENTSUB ERROR", error)
                    if sub_status == "authorization_revoked":
                        await ensure_valid_twitch_token(force=True)
                    raise RuntimeError(error)

        except asyncio.CancelledError:
            diagnostic_log("TWITCH EVENTSUB", "EventSub worker cancelled")
            raise
        except Exception as exc:
            if _twitch_auth_state.get("state") != "reauth_required":
                _set_twitch_auth_state("reconnecting", exc)
            base._eventsub_state["last_error"] = str(exc)
            diagnostic_log(
                "TWITCH EVENTSUB ERROR",
                f"Connection lost · {type(exc).__name__}: {exc} · "
                f"retry_in={reconnect_delay}s",
            )
        finally:
            base._eventsub_state["listening"] = False
            base._eventsub_state["session_id"] = None
            base._eventsub_state["subscriptions"] = []
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass

        await asyncio.sleep(reconnect_delay)
        if not connected_once:
            reconnect_delay = min(
                reconnect_delay * 2,
                TWITCH_RECONNECT_MAX_SECONDS,
            )


def hardened_ensure_eventsub_worker():
    if base._eventsub_task is None or base._eventsub_task.done():
        base._eventsub_task = asyncio.create_task(hardened_eventsub_worker())


async def _restart_eventsub_worker():
    task = base._eventsub_task
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    base._eventsub_task = None
    base._eventsub_state.update(
        {"listening": False, "session_id": None, "subscriptions": [], "last_error": None}
    )
    hardened_ensure_eventsub_worker()


_original_twitch_status = base.twitch_status


def hardened_twitch_status():
    status = _original_twitch_status()
    token = base.read_twitch_token()
    auth_state = str(_twitch_auth_state.get("state") or "disconnected")
    auth_reauth = auth_state == "reauth_required"
    if token:
        status["credential_saved"] = True
        status["reauth_required"] = bool(status.get("reauth_required") or auth_reauth)
        status["connected"] = not status["reauth_required"]
    else:
        status["credential_saved"] = False
        status["connected"] = False
    status["connection_state"] = auth_state
    status["last_validation"] = _twitch_auth_state.get("last_validation")
    status["last_refresh"] = _twitch_auth_state.get("last_refresh")
    status["refresh_count"] = int(_twitch_auth_state.get("refresh_count") or 0)
    if auth_reauth and _twitch_auth_state.get("last_error"):
        status["last_error"] = _twitch_auth_state["last_error"]
    return status


base.eventsub_worker = hardened_eventsub_worker
base.ensure_eventsub_worker = hardened_ensure_eventsub_worker
base.twitch_status = hardened_twitch_status


def _diagnostic_state():
    twitch = base.twitch_status()
    return {
        "connected": bool(twitch.get("connected")),
        "listening": bool(twitch.get("listening")),
        "session_id": str(base._eventsub_state.get("session_id") or ""),
        "last_error": str(twitch.get("last_error") or ""),
        "subscriptions": tuple(twitch.get("subscriptions") or []),
        "reauth_required": bool(twitch.get("reauth_required")),
        "connection_state": str(twitch.get("connection_state") or ""),
        "last_validation": twitch.get("last_validation"),
        "last_refresh": twitch.get("last_refresh"),
        "refresh_count": twitch.get("refresh_count"),
    }


async def diagnostic_monitor():
    previous = None
    previously_enabled = False
    last_health = 0.0
    while True:
        enabled = diagnostic_enabled()
        if enabled and not previously_enabled:
            diagnostic_log(
                "SYSTEM",
                f"Diagnostic logging enabled · build {installed_version()} · IP {_network_snapshot()}",
            )
        if enabled:
            state = _diagnostic_state()
            if previous is None:
                diagnostic_log(
                    "TWITCH",
                    f"Initial state connected={state['connected']} "
                    f"listening={state['listening']} auth_state={state['connection_state']} "
                    f"session={state['session_id'] or '-'} subscriptions={len(state['subscriptions'])} "
                    f"reauth_required={state['reauth_required']}",
                )
            else:
                if state["connected"] != previous["connected"]:
                    diagnostic_log(
                        "TWITCH",
                        f"OAuth connection state changed: "
                        f"{previous['connected']} -> {state['connected']}",
                    )
                if state["listening"] != previous["listening"]:
                    diagnostic_log(
                        "TWITCH",
                        f"EventSub listening changed: "
                        f"{previous['listening']} -> {state['listening']}",
                    )
                if state["connection_state"] != previous["connection_state"]:
                    diagnostic_log(
                        "TWITCH",
                        f"Connection state changed: "
                        f"{previous['connection_state']} -> {state['connection_state']}",
                    )
                if state["session_id"] != previous["session_id"]:
                    diagnostic_log(
                        "TWITCH",
                        f"EventSub session changed: "
                        f"{previous['session_id'] or '-'} -> {state['session_id'] or '-'}",
                    )
                if state["subscriptions"] != previous["subscriptions"]:
                    diagnostic_log(
                        "TWITCH",
                        f"Subscriptions changed: "
                        f"{len(previous['subscriptions'])} -> {len(state['subscriptions'])} · "
                        f"{', '.join(state['subscriptions']) or 'none'}",
                    )
                if state["last_error"] != previous["last_error"] and state["last_error"]:
                    diagnostic_log("TWITCH ERROR", state["last_error"])
                if state["reauth_required"] != previous["reauth_required"]:
                    diagnostic_log(
                        "TWITCH",
                        f"Reauthorization required changed: "
                        f"{previous['reauth_required']} -> {state['reauth_required']}",
                    )
                if state["refresh_count"] != previous["refresh_count"]:
                    diagnostic_log(
                        "TWITCH AUTH",
                        f"Refresh count changed: "
                        f"{previous['refresh_count']} -> {state['refresh_count']}",
                    )
            previous = state
            now = time.monotonic()
            if now - last_health >= 60:
                last_event = base._eventsub_state.get("last_event")
                age = (int(time.time()) - int(last_event)) if last_event else None
                diagnostic_log(
                    "HEALTH",
                    f"IP {_network_snapshot()} · auth_state={state['connection_state']} · "
                    f"connected={state['connected']} listening={state['listening']} "
                    f"subscriptions={len(state['subscriptions'])} "
                    f"last_event_age={age if age is not None else 'none'}s · "
                    f"refresh_count={state['refresh_count']}",
                )
                last_health = now
        else:
            previous = None
        previously_enabled = enabled
        await asyncio.sleep(0.5)


async def publish_limited(item):
    item.setdefault("ts", int(time.time()))
    base.feed.append(item)
    limit = configured_feed_limit()
    if len(base.feed) > limit:
        del base.feed[:-limit]
    dead = []
    for ws in base.clients:
        try:
            await ws.send_json(item)
        except Exception:
            dead.append(ws)
    for ws in dead:
        base.clients.discard(ws)


base.publish = publish_limited
rumble = RumbleConnector(base.publish, base.CREDS_DIR / "rumble.json")


@app.on_event("startup")
async def start_wrapper_services():
    global _diag_task, _twitch_token_task
    rumble.start()
    if _diag_task is None or _diag_task.done():
        _diag_task = asyncio.create_task(diagnostic_monitor())
    if _twitch_token_task is None or _twitch_token_task.done():
        _twitch_token_task = asyncio.create_task(twitch_token_maintenance())


@app.on_event("shutdown")
async def stop_wrapper_services():
    global _diag_task, _twitch_token_task
    diagnostic_log("SYSTEM", "FeedNode backend shutting down")
    await rumble.stop()
    for task in (_diag_task, _twitch_token_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


@app.get("/api/rumble/status")
def rumble_status():
    return rumble.public_status()


@app.post("/api/rumble/connect")
async def rumble_connect(request: Request):
    try:
        payload = await request.json()
        status = await rumble.save_url(str(payload.get("api_url") or ""))
        config = base.load_config()
        config.setdefault("platforms", {}).setdefault("rumble", {})["enabled"] = True
        base.save_config(config)
        return {"ok": True, **status}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/api/rumble/test")
async def rumble_test():
    url = rumble._read_url()
    if not url:
        return JSONResponse(
            {"ok": False, "error": "No Rumble API credential is saved"},
            status_code=400,
        )
    try:
        data = await rumble.test_url(url)
        rumble._update_metrics(data)
        rumble.state["connected"] = True
        rumble.state["last_error"] = None
        return {"ok": True, **rumble.public_status()}
    except Exception as exc:
        rumble.state["last_error"] = str(exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)


@app.post("/api/rumble/disconnect")
def rumble_disconnect():
    rumble.disconnect()
    return {"ok": True}


@app.post("/api/twitch/recover")
async def twitch_recover():
    token = await ensure_valid_twitch_token(force=True)
    if not token:
        return JSONResponse(
            {
                "ok": False,
                "reauth_required": True,
                "error": _twitch_auth_state.get("last_error")
                or "Twitch authorization required",
            },
            status_code=401,
        )
    await _restart_eventsub_worker()
    return {"ok": True, "twitch": base.twitch_status()}


@app.post("/api/twitch/disconnect-managed")
async def twitch_disconnect_managed():
    token = base.read_twitch_token() or {}
    access_token = str(token.get("access_token") or "")
    if access_token:
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                response = await client.post(
                    "https://id.twitch.tv/oauth2/revoke",
                    data={
                        "client_id": base.twitch_client_id(),
                        "token": access_token,
                    },
                )
            diagnostic_log(
                "TWITCH AUTH",
                f"Token revoke requested · status={response.status_code}",
            )
        except Exception as exc:
            diagnostic_log(
                "TWITCH AUTH ERROR",
                f"Token revoke network error: {exc}",
            )

    task = base._eventsub_task
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    base._eventsub_task = None

    base.TWITCH_TOKEN.unlink(missing_ok=True)
    base.TWITCH_PENDING.unlink(missing_ok=True)
    base._eventsub_state.update(
        {
            "listening": False,
            "session_id": None,
            "subscriptions": [],
            "last_error": None,
        }
    )
    _set_twitch_auth_state("disconnected")
    diagnostic_log(
        "TWITCH AUTH",
        "Twitch disconnected and local authorization removed",
    )
    hardened_ensure_eventsub_worker()
    return {"ok": True}


@app.get("/api/diagnostics/status")
def diagnostic_status():
    size = 0
    for path in (DIAG_OLD, DIAG_LOG):
        try:
            size += path.stat().st_size
        except Exception:
            pass
    return {
        "ok": True,
        "enabled": diagnostic_enabled(),
        "bytes": size,
        "path": "feednode-diagnostics.txt",
    }


@app.get("/api/diagnostics/export")
def diagnostic_export():
    parts = [
        f"FeedNode Diagnostic Export\nBuild: {installed_version()}\n"
        f"Generated: {datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')}\n"
        f"IP: {_network_snapshot()}\n\n"
    ]
    state = _diagnostic_state()
    parts.append("Current Twitch State\n")
    parts.append(
        f"connected={state['connected']}\n"
        f"listening={state['listening']}\n"
        f"connection_state={state['connection_state']}\n"
        f"session_id={state['session_id'] or '-'}\n"
        f"subscriptions={', '.join(state['subscriptions']) or 'none'}\n"
        f"last_error={state['last_error'] or 'none'}\n"
        f"reauth_required={state['reauth_required']}\n"
        f"last_validation={state['last_validation'] or 'none'}\n"
        f"last_refresh={state['last_refresh'] or 'none'}\n"
        f"refresh_count={state['refresh_count']}\n\n"
    )
    for label, path in (
        ("Previous rotated log", DIAG_OLD),
        ("Current log", DIAG_LOG),
    ):
        parts.append(f"===== {label} =====\n")
        try:
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            parts.append("(no log data)\n")
        parts.append("\n")
    filename = (
        f"feednode-diagnostics-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
    )
    return PlainTextResponse(
        "".join(parts),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/diagnostics/clear")
def diagnostic_clear():
    DIAG_LOG.unlink(missing_ok=True)
    DIAG_OLD.unlink(missing_ok=True)
    diagnostic_log("SYSTEM", "Diagnostic log cleared")
    return {"ok": True}


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
        data = {
            "ok": False,
            "error": raw or f"Updater exited with code {result.returncode}",
        }
    if result.returncode and data.get("ok") is not False:
        data = {
            "ok": False,
            "error": data.get("error") or raw or "Update check failed",
        }
    return data


async def _update_status(force=False):
    now = time.monotonic()
    cached = _update_cache.get("data")
    channel = configured_update_feed()
    channel_changed = cached is not None and cached.get("channel") != channel
    if (
        not force
        and not channel_changed
        and cached is not None
        and now - float(_update_cache.get("checked", 0.0)) < UPDATE_CACHE_SECONDS
    ):
        return cached
    async with _update_lock:
        now = time.monotonic()
        cached = _update_cache.get("data")
        channel = configured_update_feed()
        channel_changed = cached is not None and cached.get("channel") != channel
        if (
            not force
            and not channel_changed
            and cached is not None
            and now - float(_update_cache.get("checked", 0.0))
            < UPDATE_CACHE_SECONDS
        ):
            return cached
        data = await asyncio.to_thread(_run_updater, "check")
        _update_cache["data"] = data
        _update_cache["checked"] = time.monotonic()
        return data


def _start_detached_update(action="install"):
    return subprocess.run(
        ["sudo", str(FIRMWARE_LAUNCHER), action],
        text=True,
        capture_output=True,
        timeout=15,
    )


@app.post("/api/feed/clear")
async def clear_feed():
    base.feed.clear()
    asyncio.create_task(
        _run_system_action("restart", "feednode-kiosk.service")
    )
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
        return {
            "ok": True,
            "message": "FeedNode is already up to date",
            **check,
        }
    result = await asyncio.to_thread(_start_detached_update, "install")
    if result.returncode:
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    result.stderr
                    or result.stdout
                    or "Unable to start firmware updater"
                ).strip(),
            },
            status_code=500,
        )
    return {
        "ok": True,
        "installing": True,
        "installed": check.get("installed"),
        "available": check.get("available"),
        "channel": check.get("channel", configured_update_feed()),
        "reboot_required": check.get("reboot_required", False),
    }


@app.post("/api/update/rollback-stable")
async def rollback_stable():
    check = await _update_status(force=True)
    if not check.get("ok"):
        return JSONResponse(check, status_code=503)
    stable = check.get("stable_available")
    if not stable:
        return JSONResponse(
            {
                "ok": False,
                "error": "Unable to determine latest stable release",
            },
            status_code=503,
        )
    if check.get("installed") == stable:
        return {
            "ok": True,
            "installing": False,
            "message": "Already on latest stable",
            **check,
        }
    config = base.load_config()
    config.setdefault("system", {})["update_feed"] = "stable"
    base.save_config(config)
    _update_cache["data"] = None
    _update_cache["checked"] = 0.0
    result = await asyncio.to_thread(
        _start_detached_update,
        "rollback-stable",
    )
    if result.returncode:
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    result.stderr
                    or result.stdout
                    or "Unable to start stable rollback"
                ).strip(),
            },
            status_code=500,
        )
    return {
        "ok": True,
        "installing": True,
        "installed": check.get("installed"),
        "available": stable,
        "channel": "stable",
        "rollback": True,
    }
