"""Mod loaders: Fabric (launcher-profile JSON), Forge & NeoForge (headless installers)."""

import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

from .config import load as load_config
from .download import USER_AGENT
from .java import ensure_java
from .mojang import get_version_json, load_version, required_java_major
from .paths import versions_dir

FABRIC_META = "https://meta.fabricmc.net/v2"
FORGE_MAVEN = "https://maven.minecraftforge.net/net/minecraftforge/forge"
NEOFORGE_MAVEN = "https://maven.neoforged.net/releases/net/neoforged/neoforge"


def _get_json(url: str, timeout: int = 30):
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------- Fabric

def fabric_game_versions() -> list[str]:
    return [v["version"] for v in _get_json(f"{FABRIC_META}/versions/game")]


def fabric_loaders() -> list[dict]:
    data = _get_json(f"{FABRIC_META}/versions/loader")
    return [{"version": v["version"], "stable": v.get("stable", False)} for v in data]


def install_fabric(mc_version: str, loader_version: str, log=print) -> str:
    """Fetch the Fabric launcher profile, register it as a version. Returns version id."""
    log(f"Fabric loader {loader_version} for MC {mc_version}")
    url = f"{FABRIC_META}/versions/loader/{mc_version}/{loader_version}/profile/json"
    profile = _get_json(url)
    version_id = profile["id"]
    dest_dir = versions_dir() / version_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    with open(dest_dir / f"{version_id}.json", "w", encoding="utf-8") as fh:
        json.dump(profile, fh, indent=2)
    log(f"Registered Fabric profile: {version_id}")
    # make sure the parent (vanilla) json is cached too — inheritance needs it
    get_version_json(mc_version)
    return version_id


# ---------------------------------------------------------------- Forge

def _maven_versions(metadata_url: str) -> list[str]:
    resp = requests.get(metadata_url, timeout=30, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    return [el.text for el in root.iter("version") if el.text]


def forge_versions_for(mc_version: str) -> list[str]:
    """Forge build numbers available for a Minecraft version, newest first."""
    versions = _maven_versions(f"{FORGE_MAVEN}/maven-metadata.xml")
    out = [v.split("-", 1)[1] for v in versions if v.startswith(mc_version + "-")]
    out.reverse()  # metadata is oldest-first
    return out


def neoforge_versions() -> list[str]:
    versions = _maven_versions(f"{NEOFORGE_MAVEN}/maven-metadata.xml")
    versions.reverse()
    return versions


def _headless_install(installer_jar: str, mc_root: Path, java_path: str, log=print) -> str:
    cmd = [java_path, "-jar", installer_jar, "--installClient", str(mc_root)]
    log("Running: " + " ".join(cmd))
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=1800,
        cwd=str(mc_root),
    )
    combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    for line in combined.splitlines():
        if line.strip():
            log(line)
    if proc.returncode != 0:
        raise RuntimeError(
            f"installer exited with code {proc.returncode}. "
            f"Last output: {combined[-800:]}"
        )
    return combined


def _find_new_version_dir(before: set[str]) -> str | None:
    after = {p.name for p in versions_dir().iterdir() if p.is_dir()}
    new = sorted(after - before, key=lambda n: (versions_dir() / n).stat().st_mtime, reverse=True)
    for name in new:
        if (versions_dir() / name / f"{name}.json").is_file():
            return name
    return new[0] if new else None


def install_forge(mc_version: str, forge_version: str, log=print) -> str:
    version_full = f"{mc_version}-{forge_version}"
    installer_url = f"{FORGE_MAVEN}/{version_full}/forge-{version_full}-installer.jar"
    installer = _download_installer(installer_url, "forge", log)
    before = {p.name for p in versions_dir().iterdir() if p.is_dir()}
    java_path = ensure_java(required_java_major(load_version(mc_version)), on_progress=log)
    game_root = versions_dir().parent  # installer wants the ".minecraft-like" root
    _headless_install(installer, game_root, java_path, log)
    found = _find_new_version_dir(before)
    if not found:
        # modern forge writes into versions/forge-<mc>-<ver>
        guess = f"forge-{version_full}"
        if (versions_dir() / guess / f"{guess}.json").is_file():
            found = guess
    if not found:
        raise RuntimeError("Forge installed but new version profile was not found in versions/")
    log(f"Forge profile ready: {found}")
    get_version_json(mc_version)
    return found


def install_neoforge(neoforge_version: str, mc_version: str, log=print) -> str:
    installer_url = f"{NEOFORGE_MAVEN}/{neoforge_version}/neoforge-{neoforge_version}-installer.jar"
    installer = _download_installer(installer_url, "neoforge", log)
    before = {p.name for p in versions_dir().iterdir() if p.is_dir()}
    java_path = ensure_java(required_java_major(load_version(mc_version)), on_progress=log)
    game_root = versions_dir().parent
    _headless_install(installer, game_root, java_path, log)
    found = _find_new_version_dir(before)
    if not found:
        guess = f"neoforge-{neoforge_version}"
        if (versions_dir() / guess / f"{guess}.json").is_file():
            found = guess
    if not found:
        raise RuntimeError("NeoForge installed but new version profile was not found in versions/")
    log(f"NeoForge profile ready: {found}")
    get_version_json(mc_version)
    return found


def _download_installer(url: str, kind: str, log) -> str:
    from .download import download_file

    tmpdir = Path(os.environ.get("TEMP", "/tmp")) / "np-launcher"
    tmpdir.mkdir(parents=True, exist_ok=True)
    dest = tmpdir / url.rsplit("/", 1)[-1]
    log(f"Downloading {kind} installer…")
    download_file(url, str(dest), timeout=600)
    return str(dest)
