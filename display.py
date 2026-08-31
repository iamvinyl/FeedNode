#!/usr/bin/env python3
import io
import json
import os
import time
import threading
from collections import OrderedDict
from queue import Queue, Empty

os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import httpx
import pygame
from websockets.sync.client import connect as ws_connect

BASE_URL = os.getenv("FEEDNODE_LOCAL_URL", "http://127.0.0.1:8787")
STATE = os.getenv("FEEDNODE_STATE", "/var/lib/feednode")
CACHE_DIR = os.path.join(STATE, "cache", "native-display")
os.makedirs(CACHE_DIR, exist_ok=True)

FRAME_INTERVAL = 0.10
CONFIG_INTERVAL = 2.0
MAX_SURFACES = 128

class SurfaceCache:
    def __init__(self, limit=MAX_SURFACES):
        self.limit = limit
        self.items = OrderedDict()

    def get(self, key):
        if key in self.items:
            self.items.move_to_end(key)
            return self.items[key]
        return None

    def put(self, key, value):
        self.items[key] = value
        self.items.move_to_end(key)
        while len(self.items) > self.limit:
            self.items.popitem(last=False)

surfaces = SurfaceCache()
client = httpx.Client(timeout=3.0)

feed_queue = Queue()

def websocket_worker():
    url = BASE_URL.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
    while True:
        try:
            with ws_connect(url, open_timeout=5, close_timeout=2) as ws:
                for raw in ws:
                    try:
                        feed_queue.put(json.loads(raw))
                    except Exception:
                        pass
        except Exception:
            time.sleep(1.0)


def color(value, fallback):
    try:
        return pygame.Color(value)
    except Exception:
        return pygame.Color(fallback)


def wait_for_backend():
    while True:
        try:
            r = client.get(BASE_URL + "/api/status")
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.25)


def get_json(path, fallback):
    try:
        r = client.get(BASE_URL + path)
        r.raise_for_status()
        return r.json()
    except Exception:
        return fallback


def image_url_for_emote(fragment):
    emote = (fragment or {}).get("emote") or {}
    eid = str(emote.get("id") or "")
    if not eid:
        return None
    return f"https://static-cdn.jtvnw.net/emoticons/v2/{eid}/default/dark/2.0"


def fetch_surface(url, size):
    if not url:
        return None
    key = (url, int(size))
    cached = surfaces.get(key)
    if cached is not None:
        return cached
    try:
        r = client.get(url)
        r.raise_for_status()
        surf = pygame.image.load(io.BytesIO(r.content)).convert_alpha()
        w, h = surf.get_size()
        if w <= 0 or h <= 0:
            return None
        scale = min(size / w, size / h)
        target = (max(1, int(w * scale)), max(1, int(h * scale)))
        surf = pygame.transform.smoothscale(surf, target)
        surfaces.put(key, surf)
        return surf
    except Exception:
        return None


def load_fonts(cfg):
    style = cfg.get("style", {})
    # Native display intentionally uses a local system font; remote Google fonts remain web-UI only.
    family = style.get("font_family", "DejaVu Sans")
    path = pygame.font.match_font(family) or pygame.font.match_font("dejavusans")
    return {
        "message": pygame.font.Font(path, max(12, int(style.get("message_size", 24)))),
        "user": pygame.font.Font(path, max(12, int(style.get("username_size", 21)))),
        "event": pygame.font.Font(path, max(12, int(style.get("event_size", 20)))),
        "meta": pygame.font.Font(path, max(10, int(style.get("username_size", 21) * 0.70))),
    }


def wrap_text(font, text, max_width):
    words = str(text or "").split()
    if not words:
        return [""]
    lines, current = [], words[0]
    for word in words[1:]:
        test = current + " " + word
        if font.size(test)[0] <= max_width:
            current = test
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def draw_message_body(screen, x, y, width, item, cfg, fonts):
    style = cfg.get("style", {})
    text_color = color(style.get("text"), "#F4F7FA")
    line_h = fonts["message"].get_linesize()
    cursor_x, cursor_y = x, y
    fragments = item.get("fragments") or []
    if not fragments:
        fragments = [{"type": "text", "text": item.get("text", "")}]

    for frag in fragments:
        ftype = frag.get("type", "text")
        ftext = frag.get("text", "")
        if ftype == "emote" and frag.get("emote"):
            emote_size = max(line_h, int(style.get("message_size", 24) * 1.25))
            surf = fetch_surface(image_url_for_emote(frag), emote_size)
            if surf:
                ew, eh = surf.get_size()
                if cursor_x + ew > x + width and cursor_x > x:
                    cursor_x = x
                    cursor_y += line_h + 3
                screen.blit(surf, (cursor_x, cursor_y + max(0, (line_h - eh)//2)))
                cursor_x += ew + 4
                continue
        # Draw text fragment word-by-word so wrapping can coexist with inline emotes.
        chunks = ftext.split(" ")
        for idx, chunk in enumerate(chunks):
            token = chunk + (" " if idx < len(chunks)-1 else "")
            if token == "":
                continue
            ts = fonts["message"].render(token, True, text_color)
            tw = ts.get_width()
            if cursor_x + tw > x + width and cursor_x > x:
                cursor_x = x
                cursor_y += line_h + 3
            screen.blit(ts, (cursor_x, cursor_y))
            cursor_x += tw
    return cursor_y + line_h


def measure_item(item, cfg, fonts, width):
    style = cfg.get("style", {})
    spacing = int(style.get("spacing", 12))
    if item.get("kind") != "message":
        text = f"{item.get('user','')} {item.get('text','')}".strip()
        lines = wrap_text(fonts["event"], text, max(40, width - spacing * 2))
        return fonts["event"].get_linesize() * (len(lines) + 1) + spacing * 2
    avatar = int(style.get("avatar_size", 42)) if style.get("show_avatars", True) else 0
    body_w = max(60, width - avatar - (spacing if avatar else 0) - spacing * 2)
    # Approximation used for scrolling/layout; actual inline emotes may add one extra row.
    lines = wrap_text(fonts["message"], item.get("text", ""), body_w)
    content_h = fonts["user"].get_linesize() + 4 + max(1, len(lines)) * fonts["message"].get_linesize()
    return max(avatar, content_h) + spacing * 2


def draw_item(screen, rect, item, cfg, fonts):
    style = cfg.get("style", {})
    spacing = int(style.get("spacing", 12))
    panel = color(style.get("panel"), "#10141A")
    text = color(style.get("text"), "#F4F7FA")
    muted = color(style.get("muted"), "#8C98A6")
    accent = color(style.get("accent"), "#39E6D0")
    activity = color(style.get("activity_accent"), "#8C5CFF")
    radius = int(style.get("radius", 10))
    pygame.draw.rect(screen, panel, rect, border_radius=radius)

    x = rect.x + spacing
    y = rect.y + spacing
    if item.get("kind") != "message":
        event_name = str(item.get("event") or item.get("kind") or "activity").replace("_", " ").upper()
        ev = fonts["meta"].render(event_name, True, activity)
        screen.blit(ev, (x, y))
        y += ev.get_height() + 3
        body = f"{item.get('user','')} {item.get('text','')}".strip()
        for line in wrap_text(fonts["event"], body, rect.width - spacing * 2):
            s = fonts["event"].render(line, True, text)
            screen.blit(s, (x, y))
            y += fonts["event"].get_linesize()
        return

    avatar_size = int(style.get("avatar_size", 42))
    if style.get("show_avatars", True) and item.get("avatar"):
        av = fetch_surface(item.get("avatar"), avatar_size)
        if av:
            aw, ah = av.get_size()
            screen.blit(av, (x + (avatar_size-aw)//2, y + (avatar_size-ah)//2))
        x += avatar_size + spacing

    user_color = accent
    if style.get("use_platform_colors", True) and item.get("color"):
        user_color = color(item.get("color"), style.get("accent", "#39E6D0"))
    name = fonts["user"].render(str(item.get("user") or "User"), True, user_color)
    screen.blit(name, (x, y))
    mx = x + name.get_width() + 8
    if style.get("show_platform", True):
        meta = fonts["meta"].render(str(item.get("platform") or ""), True, muted)
        screen.blit(meta, (mx, y + max(0, name.get_height()-meta.get_height())))
    y += fonts["user"].get_linesize() + 3
    draw_message_body(screen, x, y, rect.right - spacing - x, item, cfg, fonts)


def draw_column(screen, rect, items, cfg, fonts):
    style = cfg.get("style", {})
    spacing = int(style.get("spacing", 12))
    bg = color(style.get("background"), "#080A0D")
    pygame.draw.rect(screen, bg, rect)
    heights = [measure_item(i, cfg, fonts, rect.width - spacing * 2) for i in items]
    total = sum(h + spacing for h in heights)
    y = rect.bottom - spacing - total
    if y < rect.top + spacing:
        # Discard oldest visible cards until the remaining set fits.
        while items and y < rect.top + spacing:
            total -= heights[0] + spacing
            items = items[1:]
            heights = heights[1:]
            y = rect.bottom - spacing - total
    y = max(rect.top + spacing, y)
    old_clip = screen.get_clip()
    screen.set_clip(rect)
    for item, h in zip(items, heights):
        card = pygame.Rect(rect.x + spacing, y, rect.width - spacing*2, h)
        draw_item(screen, card, item, cfg, fonts)
        y += h + spacing
    screen.set_clip(old_clip)


def render(screen, cfg, feed, fonts):
    style = cfg.get("style", {})
    layout = cfg.get("layout", {})
    screen.fill(color(style.get("background"), "#080A0D"))
    w, h = screen.get_size()
    mode = layout.get("mode", "combined")
    orientation = layout.get("orientation", "portrait")
    order = layout.get("order", "activity-first")
    ratio = max(10, min(90, int(layout.get("split_ratio", 35)))) / 100.0

    if mode == "combined":
        draw_column(screen, pygame.Rect(0, 0, w, h), list(feed), cfg, fonts)
    else:
        activities = [i for i in feed if i.get("kind") != "message"]
        messages = [i for i in feed if i.get("kind") == "message"]
        first_activity = order == "activity-first"
        if orientation == "landscape":
            cut = int(w * ratio)
            first = pygame.Rect(0, 0, cut, h)
            second = pygame.Rect(cut, 0, w-cut, h)
        else:
            cut = int(h * ratio)
            first = pygame.Rect(0, 0, w, cut)
            second = pygame.Rect(0, cut, w, h-cut)
        draw_column(screen, first, activities if first_activity else messages, cfg, fonts)
        draw_column(screen, second, messages if first_activity else activities, cfg, fonts)

    pygame.display.flip()


def main():
    wait_for_backend()
    pygame.display.init()
    pygame.font.init()
    pygame.mouse.set_visible(False)
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN | pygame.DOUBLEBUF)
    pygame.display.set_caption("FeedNode Native Display")

    cfg = get_json("/api/config", {})
    fonts = load_fonts(cfg)
    feed = get_json("/api/feed", [])
    thread = threading.Thread(target=websocket_worker, daemon=True)
    thread.start()
    last_cfg = time.monotonic()
    dirty = True

    clock = pygame.time.Clock()
    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return

        now = time.monotonic()
        if now - last_cfg >= CONFIG_INTERVAL:
            new_cfg = get_json("/api/config", cfg)
            if new_cfg != cfg:
                cfg = new_cfg
                fonts = load_fonts(cfg)
                dirty = True
            last_cfg = now

        while True:
            try:
                item = feed_queue.get_nowait()
            except Empty:
                break
            feed.append(item)
            if len(feed) > 100:
                del feed[:-100]
            dirty = True

        if dirty:
            render(screen, cfg, feed, fonts)
            dirty = False

        clock.tick(max(1, int(1.0 / FRAME_INTERVAL)))

if __name__ == "__main__":
    main()
