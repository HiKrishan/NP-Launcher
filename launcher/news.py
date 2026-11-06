"""Minecraft news feed — official launcher feed (launchercontent.mojang.com)."""

import requests

from .download import USER_AGENT

_FEED = "https://launchercontent.mojang.com/news.json"
_BASE = "https://launchercontent.mojang.com"
_FALLBACKS = (
    "https://launchercontent.mojang.com/v1/news.json",
    "https://launcher.mojang.com/v1/news.json",
)


def _absolutize(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return _BASE + url
    return url


def _normalize_entry(e: dict) -> dict:
    image = e.get("newsPageImage") or e.get("playPageImage") or {}
    img = image.get("url") if isinstance(image, dict) else ""
    date = e.get("date") or e.get("published") or ""
    tag = e.get("tag") or e.get("category") or "Minecraft"
    return {
        "title": e.get("title", "Untitled"),
        "tag": tag,
        "date": date,
        "link": _absolutize(e.get("readMoreLink") or e.get("url") or "https://www.minecraft.net/en-us/news"),
        "image": _absolutize(img or ""),
        "text": (e.get("text") or "").strip(),
    }


def get_news(limit: int = 12) -> list[dict]:
    last_error: Exception | None = None
    for url in (_FEED, *_FALLBACKS):
        try:
            resp = requests.get(url, timeout=15, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            data = resp.json()
            entries = data.get("entries", []) if isinstance(data, dict) else []
            norm = [_normalize_entry(e) for e in entries]
            # feed is oldest → newest; show newest first
            norm.sort(key=lambda n: n.get("date") or "", reverse=True)
            return norm[:limit]
        except Exception as exc:  # noqa: BLE001 — try the next endpoint
            last_error = exc
            continue
    if last_error:
        print(f"[np-launcher] news feed unavailable: {last_error}")
    return []
