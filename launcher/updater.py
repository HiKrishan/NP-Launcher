"""Self update check against GitHub Releases.

The repo slug is configurable (Settings → update_repo) because the launcher
doesn't know where it will live until it's published. Format: "owner/repo".
"""

import re

import requests

from . import __version__
from .config import load as load_config
from .download import USER_AGENT


def _parse_ver(tag: str) -> tuple:
    m = re.search(r"\d+(?:\.\d+){0,3}", tag or "")
    if not m:
        return (0,)
    return tuple(int(p) for p in m.group(0).split("."))


def check_for_update() -> dict:
    cfg = load_config()
    repo = (cfg.get("update_repo") or "").strip().strip("/")
    result = {
        "current": __version__,
        "latest": None,
        "update_available": False,
        "url": None,
        "notes": "",
        "asset_url": None,
        "repo": repo,
        "error": None,
    }
    if not repo:
        result["error"] = "no update repo configured (Settings → update_repo)"
        return result
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{repo}/releases/latest",
            timeout=15,
            headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
        )
        if resp.status_code == 404:
            result["error"] = "repo or release not found (private repo? no releases yet?)"
            return result
        resp.raise_for_status()
        data = resp.json()
        tag = data.get("tag_name", "")
        result["latest"] = tag.lstrip("vV") or tag
        result["url"] = data.get("html_url")
        result["notes"] = (data.get("body") or "")[:2000]
        for asset in data.get("assets", []):
            name = (asset.get("name") or "").lower()
            if name.endswith((".exe", ".msi", ".zip", ".7z")):
                result["asset_url"] = asset.get("browser_download_url")
                break
        if result["asset_url"] is None and data.get("assets"):
            result["asset_url"] = data["assets"][0].get("browser_download_url")
        result["update_available"] = _parse_ver(tag) > _parse_ver(__version__)
    except requests.RequestException as exc:
        result["error"] = str(exc)
    return result
