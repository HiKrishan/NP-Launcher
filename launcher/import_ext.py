"""Import Minecraft instances from other launchers (Prism/PolyMC, SKLauncher,
Modrinth App, Fast Client).

Scanning and copy-logic derived from crit-launcher
(https://github.com/snigglaberry/crit-launcher), MIT License, Copyright (c) 2026 CRIT.
Adapted to NP Launcher's instance storage.
"""

import json
import os
import re
import shutil
import sys
from pathlib import Path

from . import instances

MOD_LOADERS = ("fabric", "forge", "neoforge")

_FASTCLIENT_FABRIC_LOG_RE = re.compile(r"Loading Minecraft (\S+) with Fabric Loader (\S+)")
_QUILT_LOG_RE = re.compile(r"Loading Minecraft (\S+) with Quilt Loader (\S+)")
_FASTCLIENT_MODLAUNCHER_RE = re.compile(r"ModLauncher running: args \[(.*?)\]")
_FASTCLIENT_VERSION_ARG_RE = re.compile(r"--version,\s*([^,\]]+)")
_FASTCLIENT_TARGET_ARG_RE = re.compile(r"--launchTarget,\s*([^,\]]+)")
_FASTCLIENT_MODDED_VERSION_RE = re.compile(r"^(.+?)-(forge|neoforge)-(.+)$")
_SK_LOG_VERSION_RES = (
    re.compile(r"Minecraft Version:\s*(\d[\w.\-]*)"),
    re.compile(r"Loading Minecraft (\d[\w.\-]*)"),
    re.compile(r"--version,?\s*(\d[\w.\-]*)"),
    re.compile(r"Client\s+(\d+\.\d+(?:\.\d+)?)\b"),
)
FASTCLIENT_ROOT_SOURCE_ID = "__root__"

LAUNCHER_LABELS = {
    "prismlauncher": "Prism Launcher",
    "polymc": "PolyMC",
    "sklauncher": "SKLauncher",
    "modrinth": "Modrinth App",
    "fastclient": "Fast Client",
}


# ---------------------------------------------------------------- helpers

def _os_name() -> str:
    p = sys.platform
    if p.startswith("win"):
        return "windows"
    if p == "darwin":
        return "osx"
    return "linux"


def _home() -> Path:
    return Path.home()


def _windows_appdata() -> Path:
    env = os.environ.get("APPDATA")
    return Path(env) if env else _home() / "AppData" / "Roaming"


def _read_json_safe(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _count_dir(path: Path, suffix: str | None = None) -> int:
    if not path.is_dir():
        return 0
    try:
        if suffix:
            return sum(1 for p in path.iterdir() if p.is_file() and p.suffix.lower() == suffix)
        return sum(1 for p in path.iterdir())
    except OSError:
        return 0


def _count_images(path: Path) -> int:
    if not path.is_dir():
        return 0
    try:
        return sum(1 for p in path.iterdir()
                   if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp"))
    except OSError:
        return 0


def _maven_version(name: str, prefix: str) -> str:
    return name[len(prefix):] if name.startswith(prefix) else ""


def _loader_from_version_json(vjson: dict, mc_version: str):
    for lib in vjson.get("libraries", []):
        lname = lib.get("name", "")
        if lname.startswith("net.fabricmc:fabric-loader:"):
            return "fabric", _maven_version(lname, "net.fabricmc:fabric-loader:")
        if lname.startswith("net.minecraftforge:forge:"):
            full = _maven_version(lname, "net.minecraftforge:forge:")
            prefix = mc_version + "-"
            return "forge", (full[len(prefix):] if full.startswith(prefix) else full)
        if lname.startswith("net.neoforged:neoforge:"):
            return "neoforge", _maven_version(lname, "net.neoforged:neoforge:")
    vid = str(vjson.get("id", ""))
    m = re.match(r"^fabric-loader-([\w.]+)-", vid)
    if m:
        return "fabric", m.group(1)
    m = re.match(r"^(?:[\w.]+-)?forge-([\w.]+)$", vid)
    if m:
        return "forge", m.group(1)
    return "vanilla", None


def _loader_from_mmc_pack(components: list):
    mc_version = None
    loader, loader_version = "vanilla", None
    for c in components or []:
        uid = c.get("uid", "")
        ver = c.get("version") or c.get("cachedVersion")
        if uid == "net.minecraft":
            mc_version = ver
        elif uid == "net.fabricmc.fabric-loader":
            loader, loader_version = "fabric", ver
        elif uid == "net.minecraftforge":
            loader, loader_version = "forge", ver
        elif uid == "org.quiltmc.quilt-loader":
            loader, loader_version = "quilt", ver
        elif uid == "com.neoforged":
            loader, loader_version = "neoforge", ver
    return mc_version, loader, loader_version


def _read_first_log_line(logs_dir: Path):
    log_path = logs_dir / "latest.log"
    if not log_path.is_file():
        return None
    try:
        with log_path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line:
                    return line
    except OSError:
        return None
    return None


def _parse_fastclient_log_line(line):
    if not line:
        return None
    m = _FASTCLIENT_FABRIC_LOG_RE.search(line)
    if m:
        return "fabric", m.group(1), m.group(2)
    m = _QUILT_LOG_RE.search(line)
    if m:
        return "quilt", m.group(1), m.group(2)
    m = _FASTCLIENT_MODLAUNCHER_RE.search(line)
    if m:
        args_str = m.group(1)
        vm = _FASTCLIENT_VERSION_ARG_RE.search(args_str)
        version_str = vm.group(1).strip() if vm else None
        tm = _FASTCLIENT_TARGET_ARG_RE.search(args_str)
        target = tm.group(1).strip().lower() if tm else ""
        if version_str:
            vm2 = _FASTCLIENT_MODDED_VERSION_RE.match(version_str)
            if vm2:
                mc_version, loader, loader_version = vm2.group(1), vm2.group(2), vm2.group(3)
                return loader, mc_version, loader_version
            loader = "neoforge" if "neoforge" in target else "forge"
            return loader, version_str, None
        loader = "neoforge" if "neoforge" in target else "forge"
        return loader, None, None
    return None


def _sk_vanilla_version_from_log(logs_dir: Path):
    log_path = logs_dir / "latest.log"
    try:
        with log_path.open("r", encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f):
                if i > 80:
                    break
                for rx in _SK_LOG_VERSION_RES:
                    m = rx.search(line)
                    if m:
                        return m.group(1)
    except OSError:
        pass
    return None


def _prism_game_dir(instance_dir: Path) -> Path:
    plain = instance_dir / "minecraft"
    dotted = instance_dir / ".minecraft"
    if plain.is_dir():
        return plain
    if dotted.is_dir():
        return dotted
    return plain


def _sk_game_dir(sub: Path):
    for cand in (sub, sub / ".minecraft", sub / "minecraft"):
        if (cand / "logs" / "latest.log").is_file():
            return cand
    return None


def _fastclient_versions(versions_dir: Path) -> dict:
    versions = {}
    if not versions_dir.is_dir():
        return versions
    for vsub in versions_dir.iterdir():
        if not vsub.is_dir():
            continue
        vjson = _read_json_safe(vsub / f"{vsub.name}.json")
        if not vjson:
            for f in vsub.iterdir():
                if f.suffix == ".json":
                    vjson = _read_json_safe(f)
                    if vjson:
                        break
        if vjson:
            versions[vsub.name] = vjson
    return versions


def _fastclient_profile_index(root: Path):
    index = {}
    try:
        entries = list(root.iterdir())
    except OSError:
        return index
    for f in entries:
        if not (f.is_file() and f.suffix == ".json"):
            continue
        data = _read_json_safe(f)
        profiles_obj = data.get("profiles") if isinstance(data, dict) else None
        if not isinstance(profiles_obj, dict):
            continue
        for key, entry in profiles_obj.items():
            if not isinstance(entry, dict):
                continue
            vid = (entry.get("lastVersionId") or entry.get("versionId")
                   or entry.get("version") or entry.get("mcVersion"))
            if not vid:
                continue
            names = {key, entry.get("name")}
            game_dir = entry.get("gameDir")
            if game_dir:
                names.add(Path(game_dir).name)
            for n in names:
                if n:
                    index[n] = vid
    return index


def _fastclient_profile_version_id(profile_dir: Path):
    try:
        entries = list(profile_dir.iterdir())
    except OSError:
        return None
    for f in entries:
        if f.is_file() and f.suffix == ".json":
            data = _read_json_safe(f)
            if isinstance(data, dict):
                vid = (data.get("lastVersionId") or data.get("versionId")
                       or data.get("version") or data.get("mcVersion"))
                if vid:
                    return vid
    return None


# ------------------------------------------------------------- scanners

def _scan_prism(root: Path):
    instances_dir = root / "instances"
    if not instances_dir.is_dir():
        return []
    found = []
    for sub in sorted(instances_dir.iterdir(), key=lambda p: p.name.lower()):
        if not sub.is_dir():
            continue
        pack = _read_json_safe(sub / "mmc-pack.json")
        if not pack:
            continue
        mc_version, loader, loader_version = _loader_from_mmc_pack(pack.get("components"))
        game_dir = _prism_game_dir(sub)
        found.append({
            "source_id": sub.name,
            "name": sub.name,
            "version": mc_version,
            "loader": loader,
            "loader_version": loader_version,
            "mods_count": _count_dir(game_dir / "mods", ".jar") if loader in MOD_LOADERS else 0,
            "resourcepacks_count": _count_dir(game_dir / "resourcepacks"),
            "saves_count": _count_dir(game_dir / "saves"),
            "screenshots_count": _count_images(game_dir / "screenshots"),
            "config_count": _count_dir(game_dir / "config") if loader in MOD_LOADERS else 0,
        })
    return found


def _scan_sklauncher(root: Path):
    instances_dir = root / "instances"
    if not instances_dir.is_dir():
        return []
    found = []
    for sub in sorted(instances_dir.iterdir(), key=lambda p: p.name.lower()):
        if not sub.is_dir():
            continue
        game_dir = _sk_game_dir(sub)
        if not game_dir:
            continue
        logs_dir = game_dir / "logs"
        info = _parse_fastclient_log_line(_read_first_log_line(logs_dir))
        if info:
            loader, mc_version, loader_version = info
            if not mc_version:
                mc_version = _sk_vanilla_version_from_log(logs_dir)
        else:
            loader, loader_version = "vanilla", None
            mc_version = _sk_vanilla_version_from_log(logs_dir)
        found.append({
            "source_id": sub.name,
            "name": sub.name,
            "version": mc_version,
            "loader": loader,
            "loader_version": loader_version,
            "mods_count": _count_dir(game_dir / "mods", ".jar") if loader in MOD_LOADERS else 0,
            "resourcepacks_count": _count_dir(game_dir / "resourcepacks"),
            "saves_count": _count_dir(game_dir / "saves"),
            "screenshots_count": _count_images(game_dir / "screenshots"),
            "config_count": _count_dir(game_dir / "config") if loader in MOD_LOADERS else 0,
        })
    return found


def _scan_modrinth(root: Path):
    profiles_dir = root / "profiles"
    if not profiles_dir.is_dir():
        return []
    found = []
    for sub in sorted(profiles_dir.iterdir(), key=lambda p: p.name.lower()):
        if not sub.is_dir():
            continue
        logs_dir = sub / "logs"
        has_log = (logs_dir / "latest.log").is_file()
        info = _parse_fastclient_log_line(_read_first_log_line(logs_dir)) if has_log else None
        if info:
            loader, mc_version, loader_version = info
            if not mc_version:
                mc_version = _sk_vanilla_version_from_log(logs_dir)
        elif has_log:
            loader, loader_version = "vanilla", None
            mc_version = _sk_vanilla_version_from_log(logs_dir)
        else:
            loader, loader_version, mc_version = "unknown", None, None
        found.append({
            "source_id": sub.name,
            "name": sub.name,
            "version": mc_version,
            "loader": loader,
            "loader_version": loader_version,
            "mods_count": _count_dir(sub / "mods", ".jar") if loader in MOD_LOADERS else 0,
            "resourcepacks_count": _count_dir(sub / "resourcepacks"),
            "saves_count": _count_dir(sub / "saves"),
            "screenshots_count": _count_images(sub / "screenshots"),
            "config_count": _count_dir(sub / "config") if loader in MOD_LOADERS else 0,
        })
    return found


def _scan_fastclient(root: Path):
    versions = _fastclient_versions(root / "versions")
    found = []

    if (root / "logs" / "latest.log").is_file():
        mc_version = next(iter(versions)) if len(versions) == 1 else None
        found.append({
            "source_id": FASTCLIENT_ROOT_SOURCE_ID,
            "name": "Vanilla",
            "version": mc_version,
            "loader": "vanilla",
            "loader_version": None,
            "mods_count": 0,
            "resourcepacks_count": _count_dir(root / "resourcepacks"),
            "saves_count": _count_dir(root / "saves"),
            "screenshots_count": _count_images(root / "screenshots"),
            "config_count": 0,
        })

    profiles_dir = root / "profiles"
    if not profiles_dir.is_dir():
        return found
    profile_index = _fastclient_profile_index(root)
    for sub in sorted(profiles_dir.iterdir(), key=lambda p: p.name.lower()):
        if not sub.is_dir():
            continue
        info = _parse_fastclient_log_line(_read_first_log_line(sub / "logs"))
        if info:
            loader, mc_version, loader_version = info
        else:
            version_id = (profile_index.get(sub.name)
                          or _fastclient_profile_version_id(sub))
            if not version_id and sub.name in versions:
                version_id = sub.name
            if not version_id and len(versions) == 1:
                version_id = next(iter(versions))
            vjson = versions.get(version_id)
            if vjson:
                mc_version = vjson.get("inheritsFrom") or vjson.get("id")
                loader, loader_version = _loader_from_version_json(vjson, mc_version)
            else:
                mc_version, loader, loader_version = version_id, "unknown", None
        found.append({
            "source_id": sub.name,
            "name": sub.name,
            "version": mc_version,
            "loader": loader,
            "loader_version": loader_version,
            "mods_count": _count_dir(sub / "mods", ".jar"),
            "resourcepacks_count": _count_dir(sub / "resourcepacks"),
            "saves_count": _count_dir(sub / "saves"),
            "screenshots_count": _count_images(sub / "screenshots"),
            "config_count": _count_dir(sub / "config") if loader in MOD_LOADERS else 0,
        })
    return found


_LAUNCHER_SCANNERS = {
    "prismlauncher": _scan_prism,
    "polymc": _scan_prism,
    "sklauncher": _scan_sklauncher,
    "modrinth": _scan_modrinth,
    "fastclient": _scan_fastclient,
}


def launcher_roots(launcher: str) -> list[Path]:
    system = _os_name()
    home = _home()
    appdata = _windows_appdata()
    if launcher == "prismlauncher":
        if system == "windows":
            return [appdata / "PrismLauncher"]
        if system == "osx":
            return [home / "Library" / "Application Support" / "PrismLauncher"]
        return [home / ".local" / "share" / "PrismLauncher",
                home / ".var" / "app" / "org.prismlauncher.PrismLauncher" / "data" / "PrismLauncher"]
    if launcher == "polymc":
        if system == "windows":
            return [appdata / "PolyMC"]
        if system == "osx":
            return [home / "Library" / "Application Support" / "PolyMC"]
        return [home / ".local" / "share" / "PolyMC",
                home / ".var" / "app" / "org.polymc.PolyMC" / "data" / "polymc"]
    if launcher == "sklauncher":
        if system == "windows":
            return [appdata / ".sklauncher"]
        if system == "osx":
            return [home / "Library" / "Application Support" / ".sklauncher"]
        return [home / ".sklauncher"]
    if launcher == "fastclient":
        if system == "windows":
            return [appdata / ".fastclient"]
        if system == "osx":
            return [home / "Library" / "Application Support" / ".fastclient"]
        return [home / ".fastclient"]
    if launcher == "modrinth":
        if system == "windows":
            return [appdata / "ModrinthApp", appdata / "com.modrinth.theseus"]
        if system == "osx":
            return [home / "Library" / "Application Support" / "ModrinthApp",
                    home / "Library" / "Application Support" / "com.modrinth.theseus"]
        return [home / ".local" / "share" / "ModrinthApp",
                home / ".local" / "share" / "com.modrinth.theseus"]
    return []


def scan_all() -> list[dict]:
    sources = []
    for launcher, scanner in _LAUNCHER_SCANNERS.items():
        for root in launcher_roots(launcher):
            if not root.is_dir():
                continue
            try:
                found = scanner(root)
            except Exception as exc:  # noqa: BLE001 — one bad launcher must not break the rest
                found = []
            if found:
                sources.append({
                    "launcher": launcher,
                    "label": LAUNCHER_LABELS.get(launcher, launcher),
                    "root": str(root),
                    "instances": found,
                })
    return sources


def _find_scanned(launcher: str, source_id: str):
    scanner = _LAUNCHER_SCANNERS.get(launcher)
    if not scanner:
        return None, None
    for root in launcher_roots(launcher):
        if not root.is_dir():
            continue
        for inst in scanner(root):
            if inst["source_id"] == source_id:
                return inst, root
    return None, None


def _source_paths(launcher: str, source_id: str, root: Path):
    if launcher in ("prismlauncher", "polymc"):
        game_dir = _prism_game_dir(root / "instances" / source_id)
        return (game_dir / "options.txt", game_dir / "mods",
                game_dir / "resourcepacks", game_dir / "saves", game_dir / "config",
                game_dir / "shaderpacks", game_dir / "screenshots")
    if launcher == "sklauncher":
        game_dir = _sk_game_dir(root / "instances" / source_id) or (root / "instances" / source_id)
        return (game_dir / "options.txt", game_dir / "mods",
                game_dir / "resourcepacks", game_dir / "saves", game_dir / "config",
                game_dir / "shaderpacks", game_dir / "screenshots")
    if launcher == "modrinth":
        profile_dir = root / "profiles" / source_id
        return (profile_dir / "options.txt", profile_dir / "mods",
                profile_dir / "resourcepacks", profile_dir / "saves", profile_dir / "config",
                profile_dir / "shaderpacks", profile_dir / "screenshots")
    if launcher == "fastclient":
        profile_dir = root if source_id == FASTCLIENT_ROOT_SOURCE_ID else root / "profiles" / source_id
        return (profile_dir / "options.txt", profile_dir / "mods",
                profile_dir / "resourcepacks", profile_dir / "saves", profile_dir / "config",
                profile_dir / "shaderpacks", profile_dir / "screenshots")
    return (None,) * 7


def _copy_tree_contents(src: Path, dst: Path) -> int:
    if not src.is_dir():
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    count = 0
    for item in src.iterdir():
        target = dst / item.name
        try:
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
            count += 1
        except OSError:
            continue
    return count


def import_instance(launcher: str, source_id: str, *, dest_name: str = "",
                    existing_instance_id: str = "", options: dict | None = None,
                    log=print) -> dict:
    opts = {
        "copy_options": True, "copy_mods": True, "copy_resourcepacks": True,
        "copy_saves": False, "copy_config": True, "copy_shaderpacks": True,
        "copy_screenshots": True,
        **(options or {}),
    }
    inst, root = _find_scanned(launcher, source_id)
    if not inst:
        raise ValueError("That instance couldn't be found anymore. Try rescanning.")
    if not inst.get("version"):
        raise ValueError("Couldn't work out which Minecraft version this uses.")

    supported = inst["loader"] in ("vanilla",) or inst["loader"] in MOD_LOADERS
    dest_loader = inst["loader"] if supported else "vanilla"

    if existing_instance_id:
        existing = instances.get_instance(existing_instance_id)
        if not existing:
            raise ValueError("Destination instance not found.")
        dest_id = existing["id"]
        game_dir = instances.instance_game_dir(existing)
        log(f"Importing into existing instance '{existing.get('name')}'…")
    else:
        name = (dest_name or inst["name"]).strip()
        created = instances.create_instance(
            name, inst["version"], dest_loader,
            inst.get("loader_version") or "", min_mb=1024, max_mb=4096,
        )
        dest_id = created["id"]
        game_dir = instances.instance_game_dir(created)
        log(f"Created instance '{created['name']}' ({inst['version']}, {dest_loader})")

    options_path, mods_dir, packs_dir, saves_dir, config_dir, shaders_dir, shots_dir = _source_paths(
        launcher, source_id, root,
    )

    copied = {"options": False, "mods": 0, "resourcepacks": 0, "saves": 0,
              "config": 0, "shaderpacks": 0, "screenshots": 0}

    if opts["copy_options"] and options_path and options_path.is_file():
        shutil.copy2(options_path, game_dir / "options.txt")
        copied["options"] = True
        log("options.txt copied")
    if opts["copy_mods"] and inst["loader"] in MOD_LOADERS:
        copied["mods"] = _copy_tree_contents(mods_dir, game_dir / "mods")
        log(f"{copied['mods']} mods copied")
    if opts["copy_resourcepacks"]:
        copied["resourcepacks"] = _copy_tree_contents(packs_dir, game_dir / "resourcepacks")
    if opts["copy_saves"]:
        copied["saves"] = _copy_tree_contents(saves_dir, game_dir / "saves")
        log(f"{copied['saves']} saves copied")
    if opts["copy_config"] and inst["loader"] in MOD_LOADERS:
        copied["config"] = _copy_tree_contents(config_dir, game_dir / "config")
    if opts["copy_shaderpacks"]:
        copied["shaderpacks"] = _copy_tree_contents(shaders_dir, game_dir / "shaderpacks")
    if opts["copy_screenshots"]:
        copied["screenshots"] = _copy_tree_contents(shots_dir, game_dir / "screenshots")

    log("Import finished.")
    inst_final = instances.get_instance(dest_id)
    return {"instance": inst_final, "copied": copied}
