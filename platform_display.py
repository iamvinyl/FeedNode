#!/usr/bin/env python3
"""Platform-aware wrapper for the native FeedNode HDMI renderer."""

import stats_display as display

_base_event_draw = display._original_draw_item


def _platform_event_draw(screen, rect, item, cfg, fonts, anim_ctx):
    if item.get("kind") == "message":
        return _base_event_draw(screen, rect, item, cfg, fonts, anim_ctx)

    event_cfg = dict(cfg)
    style = dict(cfg.get("style", {}))
    fallback = style.get("event_panel", style.get("panel", "#10141A"))
    platform = str(item.get("platform") or "system").strip().lower()

    aliases = {
        "feednode": "system",
        "configuration": "system",
        "config": "system",
        "style": "system",
        "twitch": "twitch",
        "rumble": "rumble",
        "youtube": "youtube",
        "yt": "youtube",
        "kick": "kick",
    }
    platform = aliases.get(platform, platform)
    key = f"{platform}_event_panel"
    style["panel"] = style.get(key, style.get("system_event_panel", fallback) if platform == "system" else fallback)
    event_cfg["style"] = style
    return _base_event_draw(screen, rect, item, event_cfg, fonts, anim_ctx)


# stats_display.draw_item applies the generic event panel first and then calls
# _original_draw_item. Replacing that captured function here lets us select the
# final panel color by platform without altering the stable renderer internals.
display._original_draw_item = _platform_event_draw


if __name__ == "__main__":
    display.main()
