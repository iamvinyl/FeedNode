#!/usr/bin/env python3
"""FeedNode GitHub Releases updater helper."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

import httpx

REPO = os.getenv("FEEDNODE_GITHUB_REPO", "iamvinyl/FeedNode")
API = f"https://api.github.com/repos/{REPO}/releases/latest"
CURRENT_VERSION = Path(os.getenv("FEEDNODE_VERSION_FILE", "/opt/feednode/current/VERSION"))
UPDATE_SCRIPT = Path(os.getenv("FEEDNODE_UPDATE_SCRIPT", "/opt/feednode/current/scripts/update.sh"))
SPLASH_MODE = Path("/run/feednode-splash-mode")
HARDWARE = "pi-zero-2w"


def current_version() -> str:
    try:
        return CURRENT_VERSION.read_text().strip()
    except OSError:
        return "0.0.0"


def _version_tuple(value: str) -> tuple[int, ...]:
    value = value.strip().lstrip("v").split("-", 1)[0]
    return tuple(int(part) for part in value.split("."))


def _headers(accept: str = "application/vnd.github+json") -> dict[str, str]:
    headers = {
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "FeedNode-Updater",
    }
    token = (os.getenv("FEEDNODE_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _asset_map(data: dict) -> dict[str, dict[str, str]]:
    assets = {}
    for asset in data.get("assets", []):
        name = asset.get("name")
        if not name:
            continue
        assets[name] = {
            "api_url": asset.get("url") or "",
            "browser_url": asset.get("browser_download_url") or "",
        }
    return assets


def _download_asset_bytes(asset: dict[str, str], timeout: float = 30.0) -> bytes:
    api_url = asset.get("api_url") or ""
    browser_url = asset.get("browser_url") or ""
    token = (os.getenv("FEEDNODE_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN") or "").strip()

    # Private repositories must download release assets through the authenticated
    # release-assets API. browser_download_url can return 404 even with a token.
    if token and api_url:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers=_headers("application/octet-stream"),
        ) as client:
            response = client.get(api_url)
            response.raise_for_status()
            return response.content

    if browser_url:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=_headers()) as client:
            response = client.get(browser_url)
            response.raise_for_status()
            return response.content

    raise RuntimeError("Release asset has no usable download URL")


def _enter_update_display() -> bool:
    """Hand tty1 from the native renderer to the lightweight update splash."""
    if os.geteuid() != 0:
        return False
    try:
        SPLASH_MODE.write_text("UPDATING\n")
        subprocess.run(["/usr/bin/systemctl", "stop", "feednode-kiosk.service"], check=False)
        subprocess.run(["/usr/bin/systemctl", "restart", "feednode-splash.service"], check=False)
        return True
    except Exception:
        return False


def _leave_update_display() -> None:
    if os.geteuid() != 0:
        return
    try:
        SPLASH_MODE.unlink(missing_ok=True)
    except Exception:
        pass
    # Starting kiosk stops the conflicting splash service automatically.
    subprocess.run(["/usr/bin/systemctl", "restart", "feednode-kiosk.service"], check=False)


def check() -> dict:
    with httpx.Client(timeout=20, follow_redirects=True, headers=_headers()) as client:
        release = client.get(API)
        if release.status_code in (401, 403, 404):
            raise RuntimeError(
                "GitHub release check unavailable. If FeedNode is using a private repository, configure FEEDNODE_GITHUB_TOKEN."
            )
        release.raise_for_status()
        data = release.json()

    assets = _asset_map(data)
    manifest_asset = assets.get("manifest.json")
    if not manifest_asset:
        raise RuntimeError("Latest release has no manifest.json")

    try:
        manifest = json.loads(_download_asset_bytes(manifest_asset, timeout=20).decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise RuntimeError("Release manifest is not valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Release manifest is not valid JSON") from exc

    if HARDWARE not in manifest.get("hardware", []):
        raise RuntimeError("Release does not support this hardware")

    installed = current_version()
    available = manifest["version"]
    return {
        "installed": installed,
        "available": available,
        "update_available": _version_tuple(available) > _version_tuple(installed),
        "release_name": data.get("name") or data.get("tag_name"),
        "release_notes": data.get("body") or "",
        "release_url": data.get("html_url") or "",
        "manifest": manifest,
        "assets": assets,
    }


def install(info: dict) -> None:
    manifest = info["manifest"]
    asset_name = manifest["asset"]
    asset = info["assets"].get(asset_name)
    if not asset:
        raise RuntimeError(f"Release asset missing: {asset_name}")

    display_taken = _enter_update_display()
    try:
        with tempfile.TemporaryDirectory(prefix="feednode-update-") as temp:
            archive = Path(temp) / asset_name
            archive.write_bytes(_download_asset_bytes(asset, timeout=120))
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            if digest.lower() != manifest["sha256"].lower():
                raise RuntimeError("Release checksum verification failed")
            subprocess.run(["sudo", str(UPDATE_SCRIPT), str(archive), manifest["version"]], check=True)
    finally:
        if display_taken:
            _leave_update_display()


def public_info(info: dict) -> dict:
    return {
        "installed": info["installed"],
        "available": info["available"],
        "update_available": info["update_available"],
        "release_name": info["release_name"],
        "release_notes": info["release_notes"],
        "release_url": info.get("release_url", ""),
        "reboot_required": bool(info.get("manifest", {}).get("reboot_required", False)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", nargs="?", choices=("check", "install"), default="check")
    args = parser.parse_args()
    try:
        info = check()
        if args.action == "install":
            if not info["update_available"]:
                print(json.dumps({"ok": True, "installed": info["installed"], "message": "Already up to date"}))
                return 0
            install(info)
            print(json.dumps({"ok": True, "installed": info["available"], "message": "Update installed"}))
            return 0
        print(json.dumps({"ok": True, **public_info(info)}))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "installed": current_version(), "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
