"""Mojang metadata: version manifest, version JSON, libraries (with rules &
natives) and assets. Everything is resolved from the shared data directory."""

import json
import os
import platform
import re
import struct
import sys
import time
import zipfile
from pathlib import Path
from typing import Callable

import requests

from .download import USER_AGENT, download_file, download_many, file_ok
from .paths import assets_dir, cache_dir, libraries_dir, natives_dir, versions_dir

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
LIBRARIES_BASE = "https://libraries.minecraft.net/"
_MANIFEST_TTL = 6 * 3600  # seconds

ProgressCB = Callable[[str, int, int], None]  # (phase, done, total)


# --------------------------------------------------------------------------
# Version manifest
# --------------------------------------------------------------------------

def get_manifest(force: bool = False) -> dict:
    cache = cache_dir() / "version_manifest.json"
    if not force and cache.is_file():
        try:
            if time.time() - cache.stat().st_mtime < _MANIFEST_TTL:
                with open(cache, "r", encoding="utf-8") as fh:
                    return json.load(fh)
        except (OSError, json.JSONDecodeError):
            pass
    resp = requests.get(MANIFEST_URL, timeout=30, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    data = resp.json()
    try:
        tmp = str(cache) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, cache)
    except OSError:
        pass
    return data


def list_versions() -> list[dict]:
    """[{id, type, releaseTime, url}] newest first."""
    return get_manifest().get("versions", [])


def _manifest_entry(version_id: str) -> dict:
    for entry in get_manifest().get("versions", []):
        if entry.get("id") == version_id:
            return entry
    raise ValueError(f"unknown Minecraft version: {version_id}")


def get_version_json(version_id: str) -> dict:
    dest = versions_dir() / version_id / f"{version_id}.json"
    if dest.is_file():
        with open(dest, "r", encoding="utf-8") as fh:
            return json.load(fh)
    url = _manifest_entry(version_id)["url"]
    resp = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    data = resp.json()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return data


# --------------------------------------------------------------------------
# Inheritance (Fabric / Forge / NeoForge profiles extend a vanilla version)
# --------------------------------------------------------------------------

def resolve_inheritance(data: dict) -> dict:
    if not data.get("inheritsFrom"):
        return data
    parent = resolve_inheritance(get_version_json(data["inheritsFrom"]))

    merged = dict(parent)
    # libraries: child wins for identical coords, order parent -> child
    by_name: dict[str, dict] = {}
    for lib in list(parent.get("libraries", [])) + list(data.get("libraries", [])):
        name = lib.get("name", "")
        by_name[name] = lib
    merged["libraries"] = list(by_name.values())

    # arguments: lists concatenated (parent first)
    p_args, c_args = parent.get("arguments") or {}, data.get("arguments") or {}
    merged_args = {}
    for key in ("game", "jvm"):
        merged_args[key] = list(p_args.get(key, [])) + list(c_args.get(key, []))
    merged["arguments"] = merged_args

    # minecraftArguments: child overrides parent
    merged["minecraftArguments"] = (
        data.get("minecraftArguments") or parent.get("minecraftArguments")
    )
    merged["mainClass"] = data.get("mainClass") or parent.get("mainClass")
    merged["assetIndex"] = data.get("assetIndex") or parent.get("assetIndex")
    merged["assets"] = data.get("assets", parent.get("assets", "legacy"))
    merged["downloads"] = {**parent.get("downloads", {}), **data.get("downloads", {})}
    merged["javaVersion"] = data.get("javaVersion") or parent.get("javaVersion")
    merged["inheritsFrom"] = data["inheritsFrom"]
    return merged


def load_version(version_id: str) -> dict:
    return resolve_inheritance(get_version_json(version_id))


def required_java_major(data: dict) -> int:
    jv = (data or {}).get("javaVersion") or {}
    try:
        major = int(jv.get("majorVersion", 0))
    except (TypeError, ValueError):
        major = 0
    if major:
        return major
    # heuristic fallback from release time / id
    return 8


# --------------------------------------------------------------------------
# Rules (OS/arch gating declared by Mojang for libraries)
# --------------------------------------------------------------------------

def _os_name() -> str:
    p = sys.platform
    if p.startswith("win"):
        return "windows"
    if p == "darwin":
        return "osx"
    return "linux"


def _is_32bit() -> bool:
    return struct.calcsize("P") * 8 == 32


def _rule_matches(rule: dict) -> bool:
    os_rule = rule.get("os")
    if not os_rule:
        return True
    name = os_rule.get("name")
    if name and name != _os_name():
        return False
    arch = os_rule.get("arch")
    if arch == "x86" and not _is_32bit():
        return False
    if arch in ("x86_64", "x64") and _is_32bit():
        return False
    # very_old_libraries may use 'version' regex on the os version — ignore
    return True


def rules_allow(rules: list | None) -> bool:
    if not rules:
        return True
    allowed = False
    for rule in rules:
        if _rule_matches(rule):
            allowed = rule.get("action") == "allow"
    return allowed


# --------------------------------------------------------------------------
# Libraries & natives
# --------------------------------------------------------------------------

def _native_candidates() -> list[str]:
    name = _os_name()
    if name == "windows":
        return (
            ["natives-windows-64", "natives-windows"]
            if struct.calcsize("P") * 8 == 64
            else ["natives-windows-32", "natives-windows"]
        )
    if name == "osx":
        return ["natives-macos-arm64", "natives-osx-arm64", "natives-macos", "natives-osx-64", "natives-osx"]
    return (
        ["natives-linux-arm64", "natives-linux-64", "natives-linux"]
        if platform.machine().lower() in ("aarch64", "arm64")
        else ["natives-linux-64", "natives-linux"]
    )


def _lib_artifact(lib: dict) -> dict | None:
    """Resolve the single best artifact dict for this library+classifier set."""
    downloads = lib.get("downloads") or {}
    artifact = downloads.get("artifact")
    classifiers = downloads.get("classifiers") or {}
    if classifiers:
        for cand in _native_candidates():
            if cand in classifiers:
                return classifiers[cand]
        return None  # has natives for other platforms only
    return artifact


def _maven_path(name: str) -> str:
    # group:artifact:version[:classifier] -> group/artifact/version/...
    parts = name.split(":")
    group, artifact, version = parts[0], parts[1], parts[2]
    classifier = parts[3] if len(parts) > 3 else None
    base = f"{group.replace('.', '/')}/{artifact}/{version}"
    suffix = f"-{classifier}" if classifier else ""
    return f"{base}/{artifact}-{version}{suffix}.jar"


# Maven repositories probed (in order) for libraries that carry no downloads
# info (Fabric / Forge / NeoForge style profiles declare bare coords only).
_MAVEN_FALLBACKS = (
    ("net.fabricmc", "https://maven.fabricmc.net/"),
    ("net.minecraftforge", "https://maven.minecraftforge.net/"),
    ("cpw.mods", "https://maven.minecraftforge.net/"),
    ("net.neoforged", "https://maven.neoforged.net/releases/"),
)
_MAVEN_CATCHALL = (
    "https://repo1.maven.org/maven2/",
    "https://libraries.minecraft.net/",
)


def _candidate_bases(name: str) -> list[str]:
    bases = [base for prefix, base in _MAVEN_FALLBACKS if name.startswith(prefix)]
    seen = set(bases)
    for base in _MAVEN_CATCHALL:
        if base not in seen:
            bases.append(base)
    return bases


def _resolve_maven_url(name: str) -> str:
    last_err = None
    for base in _candidate_bases(name):
        url = base + _maven_path(name)
        try:
            resp = requests.head(
                url, timeout=20, allow_redirects=True, headers={"User-Agent": USER_AGENT}
            )
            if resp.status_code < 400:
                return url
            last_err = f"HTTP {resp.status_code}"
        except requests.RequestException as exc:
            last_err = exc
    raise RuntimeError(f"could not resolve library {name!r} from any known maven repo ({last_err})")


def _lib_rel_dest(lib: dict) -> tuple[str | None, str | None]:
    """Returns (rel_path, dest_path) or (None, None) if the lib is irrelevant
    here (classifiers-only library with no native build for this platform)."""
    downloads = lib.get("downloads") or {}
    if downloads.get("classifiers"):
        artifact = _lib_artifact(lib)  # None => no native for this OS/arch
        if artifact is None:
            return None, None
        rel = artifact.get("path") or _maven_path(lib.get("name", ""))
        return rel, str(libraries_dir() / rel)
    artifact = downloads.get("artifact")
    rel = (artifact or {}).get("path") or _maven_path(lib.get("name", ""))
    return rel, str(libraries_dir() / rel)


def resolve_libraries(data: dict) -> tuple[list[str], list[str]]:
    """Returns (classpath_jars, native_jars) as absolute paths (undownloaded)."""
    classpath: list[str] = []
    natives: list[str] = []
    for lib in data.get("libraries", []):
        if not rules_allow(lib.get("rules")):
            continue
        rel, dest = _lib_rel_dest(lib)
        if rel is None:
            continue
        dl = lib.get("downloads") or {}
        is_native = ("natives" in os.path.basename(rel)) or bool(dl.get("classifiers"))
        if is_native:
            natives.append(dest)
        else:
            classpath.append(dest)
    return classpath, natives


def _artifact_meta(lib: dict) -> list[dict]:
    out = []
    downloads = lib.get("downloads") or {}
    name = lib.get("name", "")
    if downloads.get("classifiers"):
        artifact = _lib_artifact(lib)
        if artifact is None:
            return out
        rel = artifact.get("path") or _maven_path(name)
        dest = str(libraries_dir() / rel)
        url = artifact.get("url") or (LIBRARIES_BASE + rel)
        return [{"url": url, "dest": dest, "sha1": artifact.get("sha1"), "size": artifact.get("size")}]

    artifact = downloads.get("artifact")
    rel = (artifact or {}).get("path") or _maven_path(name)
    dest = str(libraries_dir() / rel)
    if artifact:
        url = artifact.get("url") or LIBRARIES_BASE + rel
        out.append(
            {
                "url": url,
                "dest": dest,
                "sha1": artifact.get("sha1"),
                "size": artifact.get("size"),
            }
        )
    else:
        # bare maven coords (Fabric/Forge/NeoForge profiles): probe repos
        out.append(
            {
                "url": _resolve_maven_url(name),
                "dest": dest,
                "sha1": None,
                "size": None,
            }
        )
    return out


def ensure_libraries(
    data: dict, on_progress: ProgressCB | None = None
) -> tuple[list[str], list[str]]:
    classpath, natives = resolve_libraries(data)
    items = []
    for lib in data.get("libraries", []):
        if rules_allow(lib.get("rules")):
            items.extend(_artifact_meta(lib))

    total = len(items)
    done = 0

    def tick(_cur, _tot):
        nonlocal done
        done += 1
        if on_progress:
            on_progress("libraries", done, total)

    if on_progress:
        on_progress("libraries", 0, total)
    if items:
        download_many(items, workers=16, on_item=tick)
    return classpath, natives


def extract_natives(native_jars: list[str], version_id: str) -> Path:
    dest = natives_dir() / re.sub(r"[^A-Za-z0-9_.-]", "_", version_id)
    dest.mkdir(parents=True, exist_ok=True)
    for jar in native_jars:
        if not os.path.isfile(jar):
            continue
        try:
            with zipfile.ZipFile(jar) as zf:
                for member in zf.infolist():
                    name = member.filename
                    if name.startswith("META-INF/") or name.endswith("/"):
                        continue
                    try:
                        zf.extract(member, dest)
                    except Exception:
                        pass
        except zipfile.BadZipFile:
            continue
    return dest


# --------------------------------------------------------------------------
# Assets
# --------------------------------------------------------------------------

def ensure_assets(data: dict, on_progress: ProgressCB | None = None) -> str:
    index = data.get("assetIndex")
    assets_root = assets_dir()
    if not index:
        return str(assets_root)
    idx_id = index["id"]
    idx_dest = assets_root / "indexes" / f"{idx_id}.json"
    if not idx_dest.is_file() or (index.get("sha1") and not file_ok(str(idx_dest), index["sha1"])):
        download_file(index["url"], str(idx_dest), sha1=index.get("sha1"), expected_size=index.get("size"))
    with open(idx_dest, "r", encoding="utf-8") as fh:
        objects = json.load(fh).get("objects", {})

    items = []
    for _name, obj in objects.items():
        h = obj["hash"]
        dest = assets_root / "objects" / h[:2] / h
        items.append(
            {
                "url": f"https://resources.download.minecraft.net/{h[:2]}/{h}",
                "dest": str(dest),
                "sha1": h,
                "size": obj.get("size"),
            }
        )

    total = len(items)
    done = 0

    def tick(_c, _t):
        nonlocal done
        done += 1
        if on_progress:
            on_progress("assets", done, total)

    if on_progress:
        on_progress("assets", 0, total)
    if items:
        download_many(items, workers=16, on_item=tick)
    return str(assets_root)


def ensure_client(data: dict, on_progress: ProgressCB | None = None) -> Path:
    client = (data.get("downloads") or {}).get("client")
    if client is None:
        # fabric/neoforge profiles inherit the vanilla client jar
        raise ValueError("version JSON declares no client download (bad profile?)")
    version_id = data.get("id")
    dest = versions_dir() / version_id / f"{version_id}.jar"
    if on_progress:
        on_progress("client", 0, 1)
    download_file(
        client["url"], str(dest), sha1=client.get("sha1"), expected_size=client.get("size")
    )
    if on_progress:
        on_progress("client", 1, 1)
    return dest


def prepare_version(version_id: str, on_progress: ProgressCB | None = None) -> dict:
    """Download everything needed to play `version_id`. Returns merged version data."""
    data = load_version(version_id)
    ensure_libraries(data, on_progress)
    ensure_assets(data, on_progress)
    ensure_client(data, on_progress)
    return data
