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
HARDWARE = "pi-zero-2w"


def current_version() -> str:
    try:
        return CURRENT_VERSION.read_text().strip()
    except OSError:
        return "0.0.0"


def _version_tuple(value: str) -> tuple[int, ...]:
    value = value.strip().lstrip("v").split("-", 1)[0]
    return tuple(int(part) for part in value.split("."))


def _headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "FeedNode-Updater",
    }
    token = (os.getenv("FEEDNODE_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def check() -> dict:
    headers = _headers()
    with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as client:
        release = client.get(API)
        if release.status_code in (401, 403, 404):
            raise RuntimeError(
                "GitHub release check unavailable. If FeedNode is using a private repository, configure FEEDNODE_GITHUB_TOKEN."
            )
        release.raise_for_status()
        data = release.json()
        assets = {asset["name"]: asset["browser_download_url"] for asset in data.get("assets", [])}
        manifest_url = assets.get("manifest.json")
        if not manifest_url:
            raise RuntimeError("Latest release has no manifest.json")
        manifest_response = client.get(manifest_url)
        manifest_response.raise_for_status()
        manifest = manifest_response.json()

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
    asset_url = info["assets"].get(asset_name)
    if not asset_url:
        raise RuntimeError(f"Release asset missing: {asset_name}")

    with tempfile.TemporaryDirectory(prefix="feednode-update-") as temp:
        archive = Path(temp) / asset_name
        with httpx.Client(timeout=120, follow_redirects=True, headers=_headers()) as client:
            with client.stream("GET", asset_url) as response:
                response.raise_for_status()
                with archive.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest.lower() != manifest["sha256"].lower():
            raise RuntimeError("Release checksum verification failed")
        subprocess.run(["sudo", str(UPDATE_SCRIPT), str(archive), manifest["version"]], check=True)


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
