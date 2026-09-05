import asyncio
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx


class RumbleConnector:
    def __init__(self, publish, credential_file: Path, poll_seconds: float = 2.0):
        self.publish = publish
        self.credential_file = credential_file
        self.poll_seconds = poll_seconds
        self.task = None
        self.seen = set()
        self.seen_order = []
        self.seeded = False
        self.current_stream_id = None
        self.state = {
            "connected": False,
            "listening": False,
            "is_live": False,
            "title": "",
            "viewers": 0,
            "likes": 0,
            "followers": 0,
            "subscribers": 0,
            "last_error": None,
            "last_poll": None,
        }

    def _read_url(self):
        try:
            data = json.loads(self.credential_file.read_text())
            return str(data.get("api_url") or "").strip()
        except Exception:
            return ""

    @staticmethod
    def validate_url(value: str):
        value = str(value or "").strip()
        try:
            parsed = urlparse(value)
        except Exception:
            return None
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host or not (host == "rumble.com" or host.endswith(".rumble.com")):
            return None
        return value

    async def test_url(self, value: str):
        url = self.validate_url(value)
        if not url:
            raise RuntimeError("Enter a valid HTTPS Rumble Live Stream API URL")
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict) or "livestreams" not in data:
            raise RuntimeError("Rumble API response was not recognized")
        return data

    async def save_url(self, value: str):
        data = await self.test_url(value)
        self.credential_file.parent.mkdir(parents=True, exist_ok=True)
        self.credential_file.write_text(json.dumps({"api_url": self.validate_url(value)}, indent=2))
        try:
            self.credential_file.chmod(0o600)
        except Exception:
            pass
        self.seen.clear()
        self.seen_order.clear()
        self.seeded = False
        self.current_stream_id = None
        self.state["connected"] = True
        self.state["last_error"] = None
        self._update_metrics(data)
        return self.public_status()

    def disconnect(self):
        try:
            self.credential_file.unlink(missing_ok=True)
        except Exception:
            pass
        self.seen.clear()
        self.seen_order.clear()
        self.seeded = False
        self.current_stream_id = None
        self.state.update({
            "connected": False,
            "listening": False,
            "is_live": False,
            "title": "",
            "viewers": 0,
            "likes": 0,
            "followers": 0,
            "subscribers": 0,
            "last_error": None,
        })

    def public_status(self):
        out = dict(self.state)
        out["credential_saved"] = bool(self._read_url())
        return out

    @staticmethod
    def _key(kind, stream_id, item):
        raw = "|".join([
            str(kind),
            str(stream_id or ""),
            str(item.get("username") or item.get("user") or ""),
            str(item.get("created_on") or item.get("followed_on") or ""),
            str(item.get("text") or ""),
            str(item.get("amount_cents") or item.get("total_gifts") or ""),
        ])
        return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()

    def _remember(self, key):
        if key in self.seen:
            return False
        self.seen.add(key)
        self.seen_order.append(key)
        if len(self.seen_order) > 300:
            old = self.seen_order.pop(0)
            self.seen.discard(old)
        return True

    def _update_metrics(self, data):
        followers = data.get("followers") or {}
        subscribers = data.get("subscribers") or {}
        streams = data.get("livestreams") or []
        live = next((s for s in streams if s.get("is_live")), streams[0] if streams else None)
        self.state["followers"] = int(followers.get("num_followers") or 0)
        self.state["subscribers"] = int(subscribers.get("num_subscribers") or 0)
        if live:
            self.state["is_live"] = bool(live.get("is_live"))
            self.state["title"] = str(live.get("title") or "")
            self.state["viewers"] = int(live.get("watching_now") or 0)
            self.state["likes"] = int(live.get("likes") or 0)
        else:
            self.state["is_live"] = False
            self.state["title"] = ""
            self.state["viewers"] = 0
            self.state["likes"] = 0
        return live

    async def _publish_new(self, data):
        live = self._update_metrics(data)
        followers = data.get("followers") or {}
        subscribers = data.get("subscribers") or {}
        stream_id = str((live or {}).get("id") or "")
        if stream_id != self.current_stream_id:
            self.current_stream_id = stream_id
            self.seen.clear()
            self.seen_order.clear()
            self.seeded = False

        candidates = []
        if live:
            chat = live.get("chat") or {}
            for message in chat.get("recent_messages") or []:
                candidates.append(("message", stream_id, message))
            for rant in chat.get("recent_rants") or []:
                candidates.append(("rant", stream_id, rant))

        for follower in followers.get("recent_followers") or []:
            candidates.append(("follow", stream_id, follower))
        latest_sub = subscribers.get("latest_subscriber")
        if isinstance(latest_sub, dict):
            candidates.append(("subscription", stream_id, latest_sub))
        for gift in subscribers.get("recent_gifted_subs") or []:
            candidates.append(("gift_subs", stream_id, gift))

        # First successful poll only establishes a baseline so FeedNode does not
        # replay Rumble's recent-history window after boot/reconnect.
        if not self.seeded:
            for kind, sid, item in candidates:
                self._remember(self._key(kind, sid, item))
            self.seeded = True
            return

        for kind, sid, item in candidates:
            key = self._key(kind, sid, item)
            if not self._remember(key):
                continue
            user = item.get("username") or item.get("user") or item.get("purchased_by") or "Rumble viewer"
            badges = item.get("badges") or []
            if kind == "message":
                await self.publish({
                    "kind": "message",
                    "platform": "Rumble",
                    "user": user,
                    "text": item.get("text") or "",
                    "badges": badges,
                    "avatar": "",
                    "color": "",
                })
            elif kind == "rant":
                amount = item.get("amount_dollars")
                if amount is None:
                    amount = (float(item.get("amount_cents") or 0) / 100.0)
                text = f"${float(amount):.2f} rant"
                if item.get("text"):
                    text += f" · {item.get('text')}"
                await self.publish({"kind":"activity","platform":"Rumble","event":"rant","user":user,"text":text})
            elif kind == "follow":
                await self.publish({"kind":"activity","platform":"Rumble","event":"follow","user":user,"text":"followed"})
            elif kind == "subscription":
                await self.publish({"kind":"activity","platform":"Rumble","event":"subscription","user":user,"text":"subscribed"})
            elif kind == "gift_subs":
                total = int(item.get("total_gifts") or 1)
                await self.publish({"kind":"activity","platform":"Rumble","event":"gift_subs","user":user,"text":f"gifted {total} sub(s)"})

    async def worker(self):
        while True:
            url = self._read_url()
            if not url:
                self.state["connected"] = False
                self.state["listening"] = False
                await asyncio.sleep(3)
                continue
            try:
                data = await self.test_url(url)
                self.state["connected"] = True
                self.state["listening"] = True
                self.state["last_error"] = None
                self.state["last_poll"] = int(time.time())
                await self._publish_new(data)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state["connected"] = True
                self.state["listening"] = False
                self.state["last_error"] = str(exc)
            await asyncio.sleep(self.poll_seconds)

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.worker())

    async def stop(self):
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
