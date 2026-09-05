#!/usr/bin/env python3
"""Beta HDMI entrypoint with the FeedNode Twitch test easter egg."""
import random
import sys
import threading
import time
from pathlib import Path
from queue import Empty

import pygame

# This file lives in scripts/ inside the release. Add the release root so the
# normal display modules remain the single source of truth for feed rendering.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import stats_display as stats  # noqa: E402

base = stats.base

TRIGGER_USERS = {"iamvinyl", "chphead"}
TRIGGER_TEXT = "feednode-test"
MATRIX_SECONDS = 3.0
REVEAL_SECONDS = 2.0
TROLL_HOLD_SECONDS = 15.0
MATRIX_FPS = 20
MATRIX_CHARS = "01ABCDEFGHIJKLMNOPQRSTUVWXYZ#$%&*+-<>[]{}"

TROLL_FACE = [
    "                 __________",
    "             .-''          ``-.",
    "           .'    _..---.._     `.",
    "          /    .'  _   _  `.     \\",
    "         ;    /   (_) (_)   \\      ;",
    "         |   |  ,-.     ,-. |      |",
    "         |   | /   \\___/   \\|      |",
    "         ;    \\  .-.___.-.  /      ;",
    "          \\    `._\\_____/_.`      /",
    "           `.      `---`        .'",
    "             `-._            _.-'",
    "                  `--------`",
    "",
    "             FEEDNODE BETA TEST",
]


def is_beta_build():
    version = stats.installed_build().lower()
    return "-" in version and ("beta" in version or "alpha" in version or "rc" in version)


def is_easteregg_trigger(item):
    if not is_beta_build():
        return False
    if str(item.get("kind") or "").lower() != "message":
        return False
    if str(item.get("platform") or "").lower() != "twitch":
        return False
    user = str(item.get("user") or "").strip().lower()
    text = str(item.get("text") or "").strip().lower()
    return user in TRIGGER_USERS and text == TRIGGER_TEXT


def configured_feed_limit(cfg):
    try:
        value = int((cfg.get("system") or {}).get("max_feed_items", 100))
    except Exception:
        value = 100
    return max(10, min(250, value))


class MatrixEffect:
    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.font_size = max(14, min(26, int(min(width, height) * 0.026)))
        self.font = pygame.font.SysFont("dejavusansmono", self.font_size)
        self.face_font = self._fit_face_font(width, height)
        self.column_width = max(10, self.font.size("M")[0] + 3)
        self.columns = max(1, width // self.column_width)
        self.drops = [random.randint(-height, 0) for _ in range(self.columns)]
        self.speeds = [random.randint(max(3, self.font_size // 4), max(7, self.font_size // 2)) for _ in range(self.columns)]

    def _fit_face_font(self, width, height):
        size = max(10, min(28, int(min(width, height) * 0.026)))
        while size > 9:
            font = pygame.font.SysFont("dejavusansmono", size)
            max_w = max(font.size(line)[0] for line in TROLL_FACE)
            total_h = font.get_linesize() * len(TROLL_FACE)
            if max_w <= width * 0.90 and total_h <= height * 0.72:
                return font
            size -= 1
        return pygame.font.SysFont("dejavusansmono", 9)

    def _draw_matrix(self, surface):
        surface.fill((0, 0, 0))
        green = (40, 220, 90)
        bright = (180, 255, 195)
        dim = (15, 85, 38)
        line_h = self.font.get_linesize()
        for col in range(self.columns):
            x = col * self.column_width
            head_y = self.drops[col]
            for trail in range(7):
                y = head_y - trail * line_h
                if -line_h < y < self.height:
                    char = random.choice(MATRIX_CHARS)
                    color = bright if trail == 0 else (green if trail < 3 else dim)
                    glyph = self.font.render(char, True, color)
                    surface.blit(glyph, (x, y))
            self.drops[col] += self.speeds[col]
            if self.drops[col] - 7 * line_h > self.height and random.random() < 0.12:
                self.drops[col] = random.randint(-self.height // 2, 0)
                self.speeds[col] = random.randint(max(3, self.font_size // 4), max(7, self.font_size // 2))

    def _draw_face(self, surface, reveal_fraction):
        visible = max(0, min(len(TROLL_FACE), int(len(TROLL_FACE) * reveal_fraction + 0.999)))
        if visible <= 0:
            return
        line_h = self.face_font.get_linesize()
        total_h = line_h * len(TROLL_FACE)
        y0 = (self.height - total_h) // 2
        for idx, line in enumerate(TROLL_FACE[:visible]):
            glyph = self.face_font.render(line, True, (220, 255, 225))
            x = (self.width - glyph.get_width()) // 2
            y = y0 + idx * line_h
            # Black backing makes the revealed face readable over the rain.
            pad = 3
            pygame.draw.rect(surface, (0, 0, 0), pygame.Rect(x - pad, y, glyph.get_width() + pad * 2, line_h))
            surface.blit(glyph, (x, y))

    def draw(self, surface, elapsed):
        self._draw_matrix(surface)
        if elapsed >= MATRIX_SECONDS:
            fraction = min(1.0, (elapsed - MATRIX_SECONDS) / REVEAL_SECONDS)
            self._draw_face(surface, fraction)


def draw_effect_for_orientation(screen, cfg, effect, elapsed):
    orientation = cfg.get("layout", {}).get("orientation", "portrait")
    sw, sh = screen.get_size()
    native_landscape = sw >= sh

    if native_landscape and orientation != "landscape":
        logical = pygame.Surface((sh, sw)).convert()
        if effect.width != logical.get_width() or effect.height != logical.get_height():
            effect = MatrixEffect(*logical.get_size())
        effect.draw(logical, elapsed)
        angle = 90 if orientation == "portrait_flipped" else -90
        screen.blit(pygame.transform.rotate(logical, angle), (0, 0))
    elif not native_landscape and orientation == "landscape":
        logical = pygame.Surface((sh, sw)).convert()
        if effect.width != logical.get_width() or effect.height != logical.get_height():
            effect = MatrixEffect(*logical.get_size())
        effect.draw(logical, elapsed)
        screen.blit(pygame.transform.rotate(logical, -90), (0, 0))
    elif not native_landscape and orientation == "portrait_flipped":
        logical = pygame.Surface((sw, sh)).convert()
        if effect.width != logical.get_width() or effect.height != logical.get_height():
            effect = MatrixEffect(*logical.get_size())
        effect.draw(logical, elapsed)
        screen.blit(pygame.transform.rotate(logical, 180), (0, 0))
    else:
        if effect.width != sw or effect.height != sh:
            effect = MatrixEffect(sw, sh)
        effect.draw(screen, elapsed)
    pygame.display.flip()
    return effect


def main():
    base.wait_for_backend()
    pygame.display.init()
    pygame.font.init()
    pygame.mouse.set_visible(False)
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN | pygame.DOUBLEBUF)
    pygame.display.set_caption("FeedNode Native Display")

    cfg = base.get_json("/api/config", {})
    fonts = stats.load_fonts(cfg)
    feed = base.get_json("/api/feed", [])
    threading.Thread(target=base.websocket_worker, daemon=True).start()

    last_cfg = time.monotonic()
    last_stats_redraw = 0.0
    last_update_redraw = 0.0
    last_idle_redraw = 0.0
    dirty = True
    animation_active = False
    effect_started = None
    effect = None
    effect_duration = MATRIX_SECONDS + REVEAL_SECONDS + TROLL_HOLD_SECONDS
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
                fonts = stats.load_fonts(cfg)
                dirty = True
            last_cfg = now

        if cfg.get("style", {}).get("show_stats_bar", False) and now - last_stats_redraw >= stats.STATS_REDRAW_SECONDS:
            dirty = True
            last_stats_redraw = now
        if now - last_update_redraw >= stats.UPDATE_REDRAW_SECONDS:
            dirty = True
            last_update_redraw = now
        if not feed and not stats._twitch_auth() and now - last_idle_redraw >= 5.0:
            dirty = True
            last_idle_redraw = now

        while True:
            try:
                item = base.feed_queue.get_nowait()
            except Empty:
                break
            feed.append(item)
            limit = configured_feed_limit(cfg)
            if len(feed) > limit:
                del feed[:-limit]
            if effect_started is None and is_easteregg_trigger(item):
                effect_started = now
                orientation = cfg.get("layout", {}).get("orientation", "portrait")
                sw, sh = screen.get_size()
                logical_size = (sh, sw) if ((sw >= sh and orientation != "landscape") or (sw < sh and orientation == "landscape")) else (sw, sh)
                effect = MatrixEffect(*logical_size)
            dirty = True

        if effect_started is not None:
            elapsed = now - effect_started
            if elapsed < effect_duration:
                effect = draw_effect_for_orientation(screen, cfg, effect, elapsed)
                clock.tick(MATRIX_FPS)
                continue
            effect_started = None
            effect = None
            dirty = True

        if dirty or animation_active:
            animation_active = base.render(screen, cfg, feed, fonts)
            dirty = False
        clock.tick(max(1, int(1.0 / base.FRAME_INTERVAL)))


if __name__ == "__main__":
    main()
