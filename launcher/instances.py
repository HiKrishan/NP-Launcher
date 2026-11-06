"""Instance management — one folder per game setup under <data>/instances."""

import json
import re
import shutil
import time
from pathlib import Path

from .paths import instances_dir

_VALID_LOADERS = ("vanilla", "fabric", "forge", "neoforge")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "instance"


def _instance_path(instance_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", instance_id)
    return instances_dir() / safe


def list_instances() -> list[dict]:
    out = []
    root = instances_dir()
    for child in sorted(root.iterdir()):
        meta_file = child / "instance.json"
        if child.is_dir() and meta_file.is_file():
            try:
                with open(meta_file, "r", encoding="utf-8") as fh:
                    out.append(json.load(fh))
            except (OSError, json.JSONDecodeError):
                continue
    out.sort(key=lambda i: i.get("last_played") or i.get("created") or 0, reverse=True)
    return out


def get_instance(instance_id: str) -> dict | None:
    meta_file = _instance_path(instance_id) / "instance.json"
    if not meta_file.is_file():
        return None
    with open(meta_file, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_instance(inst: dict) -> dict:
    path = _instance_path(inst["id"])
    inst_dir = path
    inst["game_dir"] = str(inst_dir)  # saves/mods/config live here
    path.mkdir(parents=True, exist_ok=True)
    (path / "mods").mkdir(exist_ok=True)
    tmp = str(path / "instance.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(inst, fh, indent=2)
    (Path(tmp)).replace(path / "instance.json")
    return inst


def create_instance(
    name: str,
    version_id: str,
    loader: str = "vanilla",
    loader_version: str = "",
    min_mb: int = 1024,
    max_mb: int = 4096,
    jvm_args: str = "",
) -> dict:
    loader = loader if loader in _VALID_LOADERS else "vanilla"
    base = _slugify(name)
    instance_id, n = base, 2
    while _instance_path(instance_id).exists():
        instance_id = f"{base}-{n}"
        n += 1
    inst = {
        "id": instance_id,
        "name": name.strip() or instance_id,
        "version_id": version_id,
        "mc_version": version_id,
        "loader": loader,
        "loader_version": loader_version or "",
        "min_mb": int(min_mb),
        "max_mb": int(max_mb),
        "jvm_args": jvm_args or "",
        "created": int(time.time()),
        "last_played": 0,
        "mods": [],
    }
    save_instance(inst)
    # game dir skeleton
    for sub in ("saves", "config", "resourcepacks", "shaderpacks", "screenshots"):
        (_instance_path(instance_id) / sub).mkdir(exist_ok=True)
    return inst


def update_instance(instance_id: str, patch: dict) -> dict:
    inst = get_instance(instance_id)
    if not inst:
        raise ValueError(f"no such instance: {instance_id}")
    for key in ("name", "min_mb", "max_mb", "jvm_args", "version_id", "loader", "loader_version", "mods"):
        if key in patch and patch[key] is not None:
            inst[key] = patch[key]
    if patch.get("min_mb"):
        inst["min_mb"] = int(patch["min_mb"])
    if patch.get("max_mb"):
        inst["max_mb"] = int(patch["max_mb"])
    return save_instance(inst)


def delete_instance(instance_id: str) -> bool:
    path = _instance_path(instance_id)
    if path.is_dir():
        shutil.rmtree(path)
        return True
    return False


def mark_played(instance_id: str) -> None:
    inst = get_instance(instance_id)
    if inst:
        inst["last_played"] = int(time.time())
        save_instance(inst)


def instance_game_dir(inst: dict) -> Path:
    return _instance_path(inst["id"])
