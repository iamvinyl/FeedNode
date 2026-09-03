#!/usr/bin/env python3
import json
import os
import threading
import time
from pathlib import Path
from queue import Empty

import httpx
import pygame

import display as base

STATE = Path(os.getenv("FEEDNODE_STATE", "/var/lib/feednode"))
TWITCH_TOKEN = STATE / "credentials" / "twitch.json"
DISTRIBUTOR_FILE = Path(__file__).resolve().parent / "config" / "distributor.json"
STATS_REFRESH_SECONDS = 30.0
STATS_REDRAW_SECONDS = 2.0
DEFAULT_STATS_FONT_SIZE = 18

_original_load_fonts = base.load_fonts
_original_render_layout = base.render_layout
_original_draw_item = base.draw_item

_stats_lock = threading.Lock()
_stats = {
    "viewers": "--",
    "followers": "--",
    "subscribers": "--",
    "updated": 0.0,
    "refreshing": False,
}


def _read_json(path):
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


def _twitch_auth():
    token = _read_json(TWITCH_TOKEN)
    distributor = _read_json(DISTRIBUTOR_FILE)
    access_token = str(token.get("access_token") or "").strip()
    user_id = str(token.get("user_id") or "").strip()
    client_id = str(os.getenv("TWITCH_CLIENT_ID") or distributor.get("twitch_client_id") or "").strip()
    if not access_token or not user_id or not client_id:
        return None
    return access_token, user_id, client_id


def _refresh_stats_worker():
    values = {"viewers": "--", "followers": "--", "subscribers": "--"}
    try:
        auth = _twitch_auth()
        if auth:
            access_token, user_id, client_id = auth
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Client-Id": client_id,
            }
            with httpx.Client(timeout=8.0, headers=headers) as client:
                streams = client.get(
                    "https://api.twitch.tv/helix/streams",
                    params={"user_id": user_id},
                )
                if streams.status_code == 200:
                    data = streams.json().get("data") or []
                    values["viewers"] = int(data[0].get("viewer_count", 0)) if data else 0

                followers = client.get(
                    "https://api.twitch.tv/helix/channels/followers",
                    params={"broadcaster_id": user_id, "first": 1},
                )
                if followers.status_code == 200:
                    values["followers"] = int(followers.json().get("total", 0))

                subscribers = client.get(
                    "https://api.twitch.tv/helix/subscriptions",
                    params={"broadcaster_id": user_id, "first": 1},
                )
                if subscribers.status_code == 200:
                    values["subscribers"] = int(subscribers.json().get("total", 0))
    except Exception:
        pass
    finally:
        with _stats_lock:
            _stats.update(values)
            _stats["updated"] = time.monotonic()
            _stats["refreshing"] = False


def get_stats():
    now = time.monotonic()
    with _stats_lock:
        stale = now - float(_stats.get("updated", 0.0)) >= STATS_REFRESH_SECONDS
        if stale and not _stats.get("refreshing"):
            _stats["refreshing"] = True
            threading.Thread(target=_refresh_stats_worker, daemon=True).start()
        return {
            "viewers": _stats["viewers"],
            "followers": _stats["followers"],
            "subscribers": _stats["subscribers"],
        }


def _legacy_stats_size(style):
    return max(10, min(48, int(style.get("stats_bar_size", DEFAULT_STATS_FONT_SIZE))))


def load_fonts(cfg):
    fonts = _original_load_fonts(cfg)
    style = cfg.get("style", {})
    family = style.get("font_family", "DejaVu Sans")
    path = pygame.font.match_font(family) or pygame.font.match_font("dejavusans")
    legacy = _legacy_stats_size(style)
    title_size = max(8, min(48, int(style.get("stats_title_size", legacy))))
    number_size = max(10, min(64, int(style.get("stats_number_size", legacy))))
    title_font = pygame.font.Font(path, title_size)
    number_font = pygame.font.Font(path, number_size)
    title_font.set_bold(True)
    number_font.set_bold(True)
    fonts["stats_title"] = title_font
    fonts["stats_number"] = number_font
    return fonts


def _format_count(value):
    try:
        return f"{int(value):,}"
    except Exception:
        return str(value)


def stats_bar_height(cfg, fonts):
    if not cfg.get("style", {}).get("show_stats_bar", False):
        return 0
    return max(fonts["stats_title"].get_linesize(), fonts["stats_number"].get_linesize()) + 20


def draw_stats_bar(surface, cfg, fonts, height):
    style = cfg.get("style", {})
    panel = base.color(style.get("stats_bar_panel", style.get("panel")), "#10141A")
    stats_color_value = (
        style.get("stats_color")
        or style.get("stats_viewers_title_color")
        or style.get("stats_underline_color")
        or style.get("muted")
    )
    stats_accent_value = style.get("stats_accent") or style.get("stats_number_color") or style.get("text")
    stats_color = base.color(stats_color_value, "#8C98A6")
    stats_accent = base.color(stats_accent_value, "#F4F7FA")
    stats = get_stats()

    pygame.draw.rect(surface, panel, pygame.Rect(0, 0, surface.get_width(), height))
    pygame.draw.line(surface, stats_color, (0, height - 1), (surface.get_width(), height - 1), 1)

    entries = (
        ("VIEWERS", stats["viewers"]),
        ("FOLLOWERS", stats["followers"]),
        ("SUBS", stats["subscribers"]),
    )
    column_width = surface.get_width() / 3.0

    for idx, (label, value) in enumerate(entries):
        label_surf = fonts["stats_title"].render(label, True, stats_color)
        value_surf = fonts["stats_number"].render(_format_count(value), True, stats_accent)
        gap = max(5, int(fonts["stats_number"].get_height() * 0.28))
        total_width = label_surf.get_width() + gap + value_surf.get_width()
        center_x = int((idx + 0.5) * column_width)
        x = int(center_x - total_width / 2)
        center_y = height // 2
        label_y = center_y - label_surf.get_height() // 2
        value_y = center_y - value_surf.get_height() // 2
        surface.blit(label_surf, (x, label_y))
        surface.blit(value_surf, (x + label_surf.get_width() + gap, value_y))


def draw_item(screen, rect, item, cfg, fonts, anim_ctx):
    if item.get("kind") == "message":
        return _original_draw_item(screen, rect, item, cfg, fonts, anim_ctx)

    event_cfg = dict(cfg)
    event_style = dict(cfg.get("style", {}))
    event_style["panel"] = event_style.get("event_panel", event_style.get("panel", "#10141A"))
    event_cfg["style"] = event_style
    return _original_draw_item(screen, rect, item, event_cfg, fonts, anim_ctx)


def render_layout(surface, cfg, feed, fonts, anim_ctx):
    bar_height = stats_bar_height(cfg, fonts)
    if bar_height <= 0:
        return _original_render_layout(surface, cfg, feed, fonts, anim_ctx)

    w, h = surface.get_size()
    bar_height = min(bar_height, max(0, h - 80))
    if bar_height <= 0:
        return _original_render_layout(surface, cfg, feed, fonts, anim_ctx)

    content_rect = pygame.Rect(0, bar_height, w, h - bar_height)
    content_surface = surface.subsurface(content_rect)
    _original_render_layout(content_surface, cfg, feed, fonts, anim_ctx)
    draw_stats_bar(surface, cfg, fonts, bar_height)


def main():
    base.wait_for_backend()
    pygame.display.init()
    pygame.font.init()
    pygame.mouse.set_visible(False)
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN | pygame.DOUBLEBUF)
    pygame.display.set_caption("FeedNode Native Display")

    cfg = base.get_json("/api/config", {})
    fonts = load_fonts(cfg)
    feed = base.get_json("/api/feed", [])
    thread = threading.Thread(target=base.websocket_worker, daemon=True)
    thread.start()
    last_cfg = time.monotonic()
    last_stats_redraw = 0.0
    dirty = True
    animation_active = False

    clock = pygame.time.Clock()
    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return

        now = time.monotonic()
        if now - last_cfg >= base.CONFIG_INTERVAL:
            new_cfg = base.get_json("/api/config", cfg)
            if new_cfg != cfg:
                cfg = new_cfg
                fonts = load_fonts(cfg)
                dirty = True
            last_cfg = now

        if cfg.get("style", {}).get("show_stats_bar", False) and now - last_stats_redraw >= STATS_REDRAW_SECONDS:
            dirty = True
            last_stats_redraw = now

        while True:
            try:
                item = base.feed_queue.get_nowait()
            except Empty:
                break
            feed.append(item)
            if len(feed) > 100:
                del feed[:-100]
            dirty = True

        if dirty or animation_active:
            animation_active = base.render(screen, cfg, feed, fonts)
            dirty = False

        clock.tick(max(1, int(1.0 / base.FRAME_INTERVAL)))


base.load_fonts = load_fonts
base.draw_item = draw_item
base.render_layout = render_layout

if __name__ == "__main__":
    main()
