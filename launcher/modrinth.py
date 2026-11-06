"""Modrinth integration: search, install mods, install & import .mrpack modpacks."""

import json
import os
import shutil
import zipfile
from pathlib import Path

import requests

from .download import USER_AGENT, download_file, file_ok
from .instances import create_instance, instance_game_dir, update_instance
from .loaders import install_fabric, install_forge, install_neoforge

API = "https://api.modrinth.com/v2"
_HEADERS = {"User-Agent": USER_AGENT}


def _get(path: str, params: dict | None = None, timeout: int = 30):
    resp = requests.get(API + path, params=params, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def search(query: str, project_type: str = "mod", loader: str = "", game_version: str = "",
           limit: int = 24, offset: int = 0) -> dict:
    facets = [[f"project_type:{project_type}"]]
    if loader:
        facets.append([f"categories:{loader}"])
    if game_version:
        facets.append([f"versions:{game_version}"])
    data = _get("/search", params={
        "query": query,
        "limit": limit,
        "offset": offset,
        "index": "relevance",
        "facets": json.dumps(facets),
    })
    hits = []
    for h in data.get("hits", []):
        hits.append({
            "project_id": h.get("project_id"),
            "slug": h.get("slug"),
            "title": h.get("title"),
            "description": h.get("description"),
            "author": h.get("author"),
            "downloads": h.get("downloads", 0),
            "icon_url": h.get("icon_url"),
            "categories": h.get("categories", []),
            "latest_version": h.get("latest_version"),
            "follows": h.get("follows", 0),
        })
    return {"total": data.get("total_hits", 0), "hits": hits}


def get_project_versions(project_id: str, loader: str = "", game_version: str = "") -> list[dict]:
    params = {}
    if loader:
        params["loaders"] = json.dumps([loader])
    if game_version:
        params["game_versions"] = json.dumps([game_version])
    return _get(f"/project/{project_id}/version", params=params)


def _primary_file(version: dict) -> dict | None:
    files = version.get("files", [])
    if not files:
        return None
    for f in files:
        if f.get("primary"):
            return f
    return files[0]


def install_mod(instance: dict, project_id: str, version_id: str | None = None,
                log=print) -> str:
    mc = instance.get("mc_version") or instance.get("version_id")
    loader = instance.get("loader")
    if loader == "vanilla":
        raise ValueError("instance is vanilla — pick a modded instance to install mods")
    if version_id:
        version = _get(f"/version/{version_id}")
    else:
        versions = get_project_versions(project_id, loader, mc)
        if not versions:
            raise ValueError(f"no compatible {loader} build for MC {mc}")
        version = versions[0]
    f = _primary_file(version)
    if not f:
        raise ValueError("version has no files")
    dest_dir = instance_game_dir(instance) / "mods"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f["filename"]
    hashes = f.get("hashes", {})
    log(f"Downloading {f['filename']}…")
    download_file(f["url"], str(dest), sha1=hashes.get("sha1"), sha512=hashes.get("sha512"),
                  expected_size=f.get("size"), timeout=300)
    mods = list(instance.get("mods") or [])
    if f["filename"] not in mods:
        mods.append(f["filename"])
    update_instance(instance["id"], {"mods": mods})
    log(f"Installed {f['filename']}")
    return str(dest)


# ---------------------------------------------------------------- modpacks

def latest_modpack_file(project_id: str, game_version: str = "", loader: str = "") -> dict:
    versions = get_project_versions(project_id, loader, game_version)
    for v in versions:
        f = _primary_file(v)
        if f and f["filename"].endswith(".mrpack"):
            return f
    raise ValueError("no .mrpack file found for the given filters")


def _install_target(loader_key: str) -> str:
    return {"fabric-loader": "fabric", "forge": "forge", "neoforge": "neoforge"}.get(loader_key, "")


def install_mrpack_file(mrpack_path: str, log=print, name_override: str | None = None) -> dict:
    """Install a local .mrpack. Creates a new instance, downloads files, copies overrides."""
    if not os.path.isfile(mrpack_path):
        raise ValueError(f"file not found: {mrpack_path}")

    with zipfile.ZipFile(mrpack_path) as zf:
        try:
            manifest = json.loads(zf.read("modrinth.index.json"))
        except KeyError:
            raise ValueError("not a valid .mrpack (missing modrinth.index.json)")
        names = set(zf.namelist())

        deps = manifest.get("dependencies", {})
        mc_version = deps.get("minecraft")
        if not mc_version:
            raise ValueError("modpack manifest has no minecraft dependency")

        loader, loader_version = "vanilla", ""
        for key, kind in (("fabric-loader", "fabric"), ("forge", "forge"), ("neoforge", "neoforge")):
            if deps.get(key):
                loader, loader_version = kind, deps[key]
                break

        name = name_override or manifest.get("name") or Path(mrpack_path).stem
        log(f"Installing modpack '{name}' (mc {mc_version}, {loader} {loader_version})")
        inst = create_instance(name, mc_version, loader, loader_version)
        game_dir = instance_game_dir(inst)

        # files ---------------------------------------------------------
        files = manifest.get("files", [])
        total = len(files)
        done = 0
        for entry in files:
            env = entry.get("env") or {}
            if env.get("client") == "unsupported":
                done += 1
                continue  # server-only file
            rel = entry["path"].replace("\\", "/")
            if rel.startswith("/") or ".." in rel.split("/"):
                raise ValueError(f"unsafe path in mrpack: {rel}")
            dest = game_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            url = (entry.get("downloads") or [None])[0]
            if not url:
                done += 1
                continue
            hashes = entry.get("hashes", {})
            try:
                download_file(url, str(dest), sha1=hashes.get("sha1"),
                              sha512=hashes.get("sha512"),
                              expected_size=entry.get("fileSize"), timeout=600)
            except Exception as exc:  # noqa: BLE001
                log(f"  ! failed {rel}: {exc}")
            done += 1
            if done % 10 == 0 or done == total:
                log(f"  files {done}/{total}")

        # overrides -----------------------------------------------------
        for prefix in ("overrides/", "client-overrides/"):
            for n in names:
                if not n.startswith(prefix) or n.endswith("/"):
                    continue
                rel = n[len(prefix):]
                if ".." in rel.split("/"):
                    continue
                dest = game_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(n) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        log(f"Modpack files installed into {game_dir}")

    # record loader versions on the instance
    update_instance(inst["id"], {
        "version_id": mc_version,
        "loader": loader,
        "loader_version": loader_version,
    })

    # prepare the actual loader profile (not needed for vanilla)
    if loader == "fabric":
        version_id = install_fabric(mc_version, loader_version, log)
        update_instance(inst["id"], {"version_id": version_id})
    elif loader == "forge":
        version_id = install_forge(mc_version, loader_version, log)
        update_instance(inst["id"], {"version_id": version_id})
    elif loader == "neoforge":
        version_id = install_neoforge(loader_version, mc_version, log)
        update_instance(inst["id"], {"version_id": version_id})

    return inst
