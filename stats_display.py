#!/usr/bin/env python3
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from queue import Empty

import httpx
import pygame

import display as base

STATE = Path(os.getenv("FEEDNODE_STATE", "/var/lib/feednode"))
TWITCH_TOKEN = STATE / "credentials" / "twitch.json"
RUMBLE_TOKEN = STATE / "credentials" / "rumble.json"
DISTRIBUTOR_FILE = Path(__file__).resolve().parent / "config" / "distributor.json"
VERSION_FILE = Path(__file__).resolve().parent / "VERSION"
STATS_REFRESH_SECONDS = 30.0
STATS_REDRAW_SECONDS = 2.0
UPDATE_STATUS_REFRESH_SECONDS = 60.0
UPDATE_REDRAW_SECONDS = 15.0
DEFAULT_STATS_FONT_SIZE = 18
UPDATE_GREEN = "#56F28B"
BUILD_FOOTER_HEIGHT = 24

_original_load_fonts = base.load_fonts
_original_render_layout = base.render_layout
_original_draw_item = base.draw_item

STAT_KEYS = (
    "twitch_viewers", "twitch_followers", "twitch_subscribers",
    "rumble_viewers", "rumble_followers", "rumble_subscribers", "rumble_likes",
    "youtube_viewers", "youtube_subscribers", "youtube_members", "youtube_likes",
    "kick_viewers", "kick_followers", "kick_subscribers", "kick_likes",
)
_stats_lock = threading.Lock()
_stats = {key: "--" for key in STAT_KEYS}
_stats.update({"updated": 0.0, "refreshing": False})
_update_lock = threading.Lock()
_update_status = {"available": False, "version": "", "updated": 0.0, "refreshing": False}


def _read_json(path):
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


def installed_build():
    try:
        return VERSION_FILE.read_text().strip()
    except Exception:
        return "unknown"


def _twitch_auth():
    token = _read_json(TWITCH_TOKEN)
    distributor = _read_json(DISTRIBUTOR_FILE)
    access_token = str(token.get("access_token") or "").strip()
    user_id = str(token.get("user_id") or "").strip()
    client_id = str(os.getenv("TWITCH_CLIENT_ID") or distributor.get("twitch_client_id") or "").strip()
    if not access_token or not user_id or not client_id:
        return None
    return access_token, user_id, client_id


def _rumble_configured():
    return bool(_read_json(RUMBLE_TOKEN).get("api_url"))


def _platform_configured(name):
    if name == "twitch":
        return bool(_twitch_auth())
    if name == "rumble":
        return _rumble_configured()
    # YouTube and Kick rows are already configurable, but their connectors do
    # not exist yet. Keeping them false prevents empty rows from consuming HDMI.
    return False


def _active_connection():
    try:
        result = subprocess.run(["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", "wlan0"], text=True, capture_output=True, timeout=2)
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _lan_ip():
    try:
        result = subprocess.run(["hostname", "-I"], text=True, capture_output=True, timeout=2)
        for value in result.stdout.split():
            if value and ":" not in value:
                return value
    except Exception:
        pass
    return ""


def _refresh_stats_worker():
    values = {key: "--" for key in STAT_KEYS}
    try:
        auth = _twitch_auth()
        if auth:
            access_token, user_id, client_id = auth
            headers = {"Authorization": f"Bearer {access_token}", "Client-Id": client_id}
            with httpx.Client(timeout=8.0, headers=headers) as client:
                streams = client.get("https://api.twitch.tv/helix/streams", params={"user_id": user_id})
                if streams.status_code == 200:
                    data = streams.json().get("data") or []
                    values["twitch_viewers"] = int(data[0].get("viewer_count", 0)) if data else 0
                followers = client.get("https://api.twitch.tv/helix/channels/followers", params={"broadcaster_id": user_id, "first": 1})
                if followers.status_code == 200:
                    values["twitch_followers"] = int(followers.json().get("total", 0))
                subscribers = client.get("https://api.twitch.tv/helix/subscriptions", params={"broadcaster_id": user_id, "first": 1})
                if subscribers.status_code == 200:
                    values["twitch_subscribers"] = int(subscribers.json().get("total", 0))

        with httpx.Client(timeout=5.0) as client:
            rumble = client.get("http://127.0.0.1:8787/api/rumble/status")
            if rumble.status_code == 200:
                data = rumble.json()
                if data.get("credential_saved"):
                    values["rumble_viewers"] = int(data.get("viewers") or 0)
                    values["rumble_followers"] = int(data.get("followers") or 0)
                    values["rumble_subscribers"] = int(data.get("subscribers") or 0)
                    values["rumble_likes"] = int(data.get("likes") or 0)
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
        return {key: _stats[key] for key in STAT_KEYS}


def _refresh_update_worker():
    available = False
    version = ""
    try:
        with httpx.Client(timeout=8.0) as client:
            response = client.get("http://127.0.0.1:8787/api/update/check")
            if response.status_code == 200:
                data = response.json()
                available = bool(data.get("ok") and data.get("update_available"))
                version = str(data.get("available") or "") if available else ""
    except Exception:
        pass
    finally:
        with _update_lock:
            _update_status.update({"available": available, "version": version, "updated": time.monotonic(), "refreshing": False})


def get_update_available():
    now = time.monotonic()
    with _update_lock:
        stale = now - float(_update_status.get("updated", 0.0)) >= UPDATE_STATUS_REFRESH_SECONDS
        if stale and not _update_status.get("refreshing"):
            _update_status["refreshing"] = True
            threading.Thread(target=_refresh_update_worker, daemon=True).start()
        return bool(_update_status.get("available"))


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
    build_font = pygame.font.Font(path, 11)
    idle_brand = pygame.font.Font(path, 34)
    idle_subtitle = pygame.font.Font(path, 18)
    idle_status = pygame.font.Font(path, 24)
    idle_body = pygame.font.Font(path, 17)
    title_font.set_bold(True)
    number_font.set_bold(True)
    build_font.set_bold(True)
    idle_brand.set_bold(True)
    idle_status.set_bold(True)
    fonts.update({"stats_title": title_font, "stats_number": number_font, "build_footer": build_font, "idle_brand": idle_brand, "idle_subtitle": idle_subtitle, "idle_status": idle_status, "idle_body": idle_body})
    return fonts


def _format_count(value):
    try:
        return f"{int(value):,}"
    except Exception:
        return str(value)


def _numeric(value):
    try:
        return int(value)
    except Exception:
        return 0


def _combined_entries(style, stats):
    entries = []
    if style.get("combined_stats_viewers", style.get("show_stats_viewers", True)):
        entries.append(("VIEWERS", _numeric(stats["twitch_viewers"]) + _numeric(stats["rumble_viewers"]) + _numeric(stats["youtube_viewers"])))
    if style.get("combined_stats_followers", style.get("show_stats_followers", True)):
        entries.append(("FOLLOWERS", _numeric(stats["twitch_followers"]) + _numeric(stats["rumble_followers"]) + _numeric(stats["youtube_subscribers"])))
    if style.get("combined_stats_subscribers", style.get("show_stats_subscribers", True)):
        entries.append(("SUBSCRIBERS", _numeric(stats["twitch_subscribers"]) + _numeric(stats["rumble_subscribers"]) + _numeric(stats["youtube_members"])))
    if style.get("combined_stats_likes", style.get("show_stats_likes", False)):
        entries.append(("LIKES", _numeric(stats["rumble_likes"]) + _numeric(stats["youtube_likes"])))
    return entries


def _platform_entries(style, stats, platform):
    if platform == "twitch":
        mapping = (("twitch_stats_viewers", "VIEWERS", "twitch_viewers"), ("twitch_stats_followers", "FOLLOWERS", "twitch_followers"), ("twitch_stats_subscribers", "SUBS", "twitch_subscribers"))
    elif platform == "rumble":
        mapping = (("rumble_stats_viewers", "VIEWERS", "rumble_viewers"), ("rumble_stats_followers", "FOLLOWERS", "rumble_followers"), ("rumble_stats_subscribers", "SUBSCRIBERS", "rumble_subscribers"), ("rumble_stats_likes", "LIKES", "rumble_likes"))
    elif platform == "youtube":
        mapping = (("youtube_stats_viewers", "VIEWERS", "youtube_viewers"), ("youtube_stats_subscribers", "SUBSCRIBERS", "youtube_subscribers"), ("youtube_stats_members", "MEMBERS", "youtube_members"), ("youtube_stats_likes", "LIKES", "youtube_likes"))
    else:
        mapping = (("kick_stats_viewers", "VIEWERS", "kick_viewers"), ("kick_stats_followers", "FOLLOWERS", "kick_followers"), ("kick_stats_subscribers", "SUBSCRIBERS", "kick_subscribers"), ("kick_stats_likes", "LIKES", "kick_likes"))
    return [(label, stats[key]) for option, label, key in mapping if style.get(option, True)]


def stats_rows(cfg, stats=None):
    style = cfg.get("style", {})
    mode = str(style.get("stats_mode") or ("platform" if style.get("combine_viewers") is False else "combined")).lower()
    stats = stats or {key: "--" for key in STAT_KEYS}
    if mode != "platform":
        entries = _combined_entries(style, stats)
        return [("COMBINED", entries, style.get("stats_bar_panel", style.get("panel", "#10141A")), style.get("stats_accent", style.get("text", "#F4F7FA")))] if entries else []

    rows = []
    for platform, label, panel_key, accent_key in (
        ("twitch", "TWITCH", "twitch_event_panel", "twitch_event_accent"),
        ("rumble", "RUMBLE", "rumble_event_panel", "rumble_event_accent"),
        ("youtube", "YOUTUBE", "youtube_event_panel", "youtube_event_accent"),
        ("kick", "KICK", "kick_event_panel", "kick_event_accent"),
    ):
        if not _platform_configured(platform):
            continue
        entries = _platform_entries(style, stats, platform)
        if entries:
            rows.append((label, entries, style.get(panel_key, style.get("stats_bar_panel", "#10141A")), style.get(accent_key, style.get("stats_accent", "#F4F7FA"))))
    return rows


def stats_row_height(fonts):
    text_height = max(fonts["stats_title"].get_linesize(), fonts["stats_number"].get_linesize())
    padding = max(14, int(text_height * 0.55))
    return text_height + padding


def stats_bar_height(cfg, fonts):
    if not cfg.get("style", {}).get("show_stats_bar", False):
        return 0
    rows = stats_rows(cfg)
    return stats_row_height(fonts) * len(rows)


def build_footer_height(cfg):
    return BUILD_FOOTER_HEIGHT if cfg.get("style", {}).get("show_build_footer", False) else 0


def _draw_stats_row(surface, cfg, fonts, rect, platform_label, entries, panel_value, accent_value):
    style = cfg.get("style", {})
    panel = base.color(panel_value, "#10141A")
    stats_color = base.color(style.get("stats_color") or style.get("muted"), "#8C98A6")
    accent = base.color(accent_value, style.get("stats_accent", "#F4F7FA"))
    pygame.draw.rect(surface, panel, rect)
    pygame.draw.line(surface, stats_color, (rect.left, rect.bottom - 1), (rect.right, rect.bottom - 1), 1)
    if not entries:
        return

    label_margin = max(12, int(rect.width * 0.018))
    platform_surf = fonts["stats_title"].render(platform_label, True, accent)
    platform_area = min(max(platform_surf.get_width() + label_margin * 2, int(rect.width * 0.14)), int(rect.width * 0.25))
    platform_y = rect.y + (rect.height - platform_surf.get_height()) // 2
    surface.blit(platform_surf, (rect.x + label_margin, platform_y))

    content_x = rect.x + platform_area
    content_width = max(1, rect.width - platform_area)
    column_width = content_width / float(len(entries))
    for idx, (label, value) in enumerate(entries):
        label_surf = fonts["stats_title"].render(label, True, stats_color)
        value_surf = fonts["stats_number"].render(_format_count(value), True, accent)
        gap = max(5, int(fonts["stats_number"].get_height() * 0.28))
        total_width = label_surf.get_width() + gap + value_surf.get_width()
        center_x = int(content_x + (idx + 0.5) * column_width)
        x = int(center_x - total_width / 2)
        center_y = rect.y + rect.height // 2
        surface.blit(label_surf, (x, center_y - label_surf.get_height() // 2))
        surface.blit(value_surf, (x + label_surf.get_width() + gap, center_y - value_surf.get_height() // 2))


def draw_stats_bar(surface, cfg, fonts, height):
    stats = get_stats()
    rows = stats_rows(cfg, stats)
    if not rows:
        return
    row_height = stats_row_height(fonts)
    y = 0
    for platform_label, entries, panel_value, accent_value in rows:
        remaining = max(1, min(row_height, height - y))
        _draw_stats_row(surface, cfg, fonts, pygame.Rect(0, y, surface.get_width(), remaining), platform_label, entries, panel_value, accent_value)
        y += row_height
        if y >= height:
            break


def draw_build_footer(surface, cfg, fonts, height):
    if height <= 0:
        return
    style = cfg.get("style", {})
    panel = base.color(style.get("stats_bar_panel", style.get("panel")), "#10141A")
    muted = base.color(style.get("muted"), "#8C98A6")
    y = surface.get_height() - height
    pygame.draw.rect(surface, panel, pygame.Rect(0, y, surface.get_width(), height))
    text = fonts["build_footer"].render(f"FEEDNODE · BUILD v{installed_build()}", True, muted)
    surface.blit(text, (max(10, surface.get_width() - text.get_width() - 12), y + (height - text.get_height()) // 2))


def draw_update_icon(surface, top_offset=0):
    if not get_update_available():
        return
    w, h = surface.get_size()
    radius = max(11, min(18, int(min(w, h) * 0.018)))
    margin = max(10, radius)
    cx = w - margin - radius
    cy = top_offset + margin + radius
    green = base.color(UPDATE_GREEN, UPDATE_GREEN)
    thickness = max(2, radius // 5)
    pygame.draw.circle(surface, green, (cx, cy), radius, thickness)
    shaft_top = cy - radius // 2
    shaft_bottom = cy + radius // 3
    pygame.draw.line(surface, green, (cx, shaft_bottom), (cx, shaft_top), thickness)
    pygame.draw.line(surface, green, (cx, shaft_top), (cx - radius // 3, cy - radius // 6), thickness)
    pygame.draw.line(surface, green, (cx, shaft_top), (cx + radius // 3, cy - radius // 6), thickness)


def draw_idle_state(surface, cfg, fonts):
    style = cfg.get("style", {})
    background = base.color(style.get("background"), "#080A0D")
    text = base.color(style.get("text"), "#F4F7FA")
    muted = base.color(style.get("muted"), "#8C98A6")
    accent = base.color(style.get("system_event_accent", style.get("activity_accent")), "#8C5CFF")
    surface.fill(background)
    active = _active_connection()
    ip = _lan_ip()
    if active == "feednode-setup":
        status = "SETUP REQUIRED"
        lines = ["Connect to FeedNode-Setup", "http://10.42.0.1/setup"]
    else:
        status = "PLATFORMS NOT CONNECTED"
        address = f"http://{ip}/settings" if ip else "http://feednode.local"
        lines = ["Open FeedNode settings", address]
    brand = fonts["idle_brand"].render("FEEDNODE", True, text)
    subtitle = fonts["idle_subtitle"].render("UNIFIED CHAT FEED", True, muted)
    status_surf = fonts["idle_status"].render(status, True, accent)
    body = [fonts["idle_body"].render(line, True, text if idx == 1 else muted) for idx, line in enumerate(lines)]
    total_h = brand.get_height() + 8 + subtitle.get_height() + 42 + status_surf.get_height() + 20 + sum(item.get_height() + 8 for item in body)
    y = max(24, (surface.get_height() - total_h) // 2)
    for item, gap in ((brand, 8), (subtitle, 42), (status_surf, 20)):
        surface.blit(item, ((surface.get_width() - item.get_width()) // 2, y))
        y += item.get_height() + gap
    for item in body:
        surface.blit(item, ((surface.get_width() - item.get_width()) // 2, y))
        y += item.get_height() + 8


def _platform_style_keys(item):
    platform = str(item.get("platform") or "system").strip().lower()
    if platform == "twitch":
        return "twitch_event_panel", "twitch_event_accent"
    if platform == "rumble":
        return "rumble_event_panel", "rumble_event_accent"
    if platform in {"youtube", "you tube"}:
        return "youtube_event_panel", "youtube_event_accent"
    if platform == "kick":
        return "kick_event_panel", "kick_event_accent"
    return "system_event_panel", "system_event_accent"


def draw_item(screen, rect, item, cfg, fonts, anim_ctx):
    if item.get("kind") == "message":
        return _original_draw_item(screen, rect, item, cfg, fonts, anim_ctx)
    event_cfg = dict(cfg)
    event_style = dict(cfg.get("style", {}))
    panel_key, accent_key = _platform_style_keys(item)
    event_style["panel"] = event_style.get(panel_key, event_style.get("event_panel", event_style.get("panel", "#10141A")))
    event_style["activity_accent"] = event_style.get(accent_key, event_style.get("activity_accent", "#8C5CFF"))
    event_cfg["style"] = event_style
    return _original_draw_item(screen, rect, item, event_cfg, fonts, anim_ctx)


def render_layout(surface, cfg, feed, fonts, anim_ctx):
    if not feed and not _twitch_auth() and not _rumble_configured():
        draw_idle_state(surface, cfg, fonts)
        return
    bar_height = stats_bar_height(cfg, fonts)
    footer_height = build_footer_height(cfg)
    w, h = surface.get_size()
    if bar_height > 0:
        bar_height = min(bar_height, max(0, h - 80))
    if footer_height > 0:
        footer_height = min(footer_height, max(0, h - bar_height - 60))
    content_height = h - bar_height - footer_height
    if content_height > 0 and (bar_height > 0 or footer_height > 0):
        content_surface = surface.subsurface(pygame.Rect(0, bar_height, w, content_height))
        _original_render_layout(content_surface, cfg, feed, fonts, anim_ctx)
    else:
        _original_render_layout(surface, cfg, feed, fonts, anim_ctx)
    if bar_height > 0:
        draw_stats_bar(surface, cfg, fonts, bar_height)
    if footer_height > 0:
        draw_build_footer(surface, cfg, fonts, footer_height)
    draw_update_icon(surface, bar_height)


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
    threading.Thread(target=base.websocket_worker, daemon=True).start()
    last_cfg = time.monotonic()
    last_stats_redraw = 0.0
    last_update_redraw = 0.0
    last_idle_redraw = 0.0
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
        if now - last_update_redraw >= UPDATE_REDRAW_SECONDS:
            dirty = True
            last_update_redraw = now
        if not feed and not _twitch_auth() and not _rumble_configured() and now - last_idle_redraw >= 5.0:
            dirty = True
            last_idle_redraw = now
        while True:
            try:
                item = base.feed_queue.get_nowait()
            except Empty:
                break
            feed.append(item)
            try:
                limit = max(10, min(250, int((cfg.get("system") or {}).get("max_feed_items", 100))))
            except Exception:
                limit = 100
            if len(feed) > limit:
                del feed[:-limit]
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
