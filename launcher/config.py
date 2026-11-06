"""Global launcher settings, persisted to <data>/config.json."""

import json
import os
import threading

from . import __version__
from .paths import data_dir

_LOCK = threading.Lock()

_DEFAULTS = {
    "java_path": "",               # empty = auto-detect / auto-download
    "offline_username": "Player",
    "default_min_mb": 1024,
    "default_max_mb": 4096,
    "jvm_args": "",                # extra args appended for every launch
    "update_repo": "",             # GitHub slug "owner/repo" used by the update checker
    "news_enabled": True,
    "last_instance": "",
    "window_width": 1600,
    "window_height": 900,
}


def _path():
    return data_dir() / "config.json"


def load() -> dict:
    with _LOCK:
        cfg = dict(_DEFAULTS)
        try:
            with open(_path(), "r", encoding="utf-8") as fh:
                cfg.update(json.load(fh))
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError):
            pass
        return cfg


def save(cfg: dict) -> dict:
    with _LOCK:
        merged = dict(_DEFAULTS)
        merged.update({k: v for k, v in cfg.items() if k in _DEFAULTS})
        merged["launcher_version"] = __version__
        tmp = str(_path()) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=2)
        os.replace(tmp, _path())
        return {k: merged[k] for k in _DEFAULTS}


def get(key: str, default=None):
    return load().get(key, default)
