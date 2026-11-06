"""Java discovery + on-demand Temurin JRE downloads (via Adoptium API)."""

import glob
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import requests

from .config import load as load_config
from .download import USER_AGENT
from .paths import runtimes_dir


def _parse_major(version_output: str) -> int | None:
    m = re.search(r'version "([^"]+)"', version_output)
    if not m:
        return None
    parts = m.group(1).split(".")
    try:
        first = int(parts[0])
    except ValueError:
        return None
    if first == 1 and len(parts) > 1:  # legacy "1.8.0_392"
        try:
            return int(parts[1])
        except ValueError:
            return None
    return first


def probe_java(exe: str) -> dict | None:
    try:
        out = subprocess.run(
            [exe, "-version"], capture_output=True, text=True, timeout=15
        )
        text = (out.stderr or "") + (out.stdout or "")
        major = _parse_major(text)
        if major:
            return {"path": exe, "major": major, "raw": text.strip().splitlines()[0] if text.strip() else ""}
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _candidate_exes() -> list[str]:
    cands: list[str] = []
    for name in ("java", "javaw"):
        p = shutil.which(name)
        if p:
            cands.append(p)
    if sys.platform.startswith("win"):
        patterns = [
            r"C:\Program Files\Java\*\bin\java.exe",
            r"C:\Program Files\Eclipse Adoptium\*\bin\java.exe",
            r"C:\Program Files\Microsoft\jdk*\bin\java.exe",
            r"C:\Program Files\Zulu\*\bin\java.exe",
            r"C:\Program Files\BellSoft\*\bin\java.exe",
            r"C:\Program Files\Amazon Corretto\*\bin\java.exe",
            r"C:\Program Files (x86)\Java\*\bin\java.exe",
        ]
        # Temurin we downloaded ourselves
        patterns.append(str(runtimes_dir() / "temurin-*" / "*" / "bin" / "java.exe"))
        patterns.append(str(runtimes_dir() / "temurin-*" / "bin" / "java.exe"))
        for pat in patterns:
            cands.extend(glob.glob(pat))
        # registry (best effort)
        try:
            import winreg  # type: ignore

            for hive, key in (
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\JavaSoft\JDK"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\JavaSoft\JRE"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\JavaSoft\JDK"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\JavaSoft\JRE"),
            ):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        i = 0
                        while True:
                            try:
                                ver = winreg.EnumKey(k, i)
                                i += 1
                            except OSError:
                                break
                            try:
                                with winreg.OpenKey(k, ver) as vk:
                                    home, _ = winreg.QueryValueEx(vk, "JavaHome")
                                    exe = os.path.join(home, "bin", "java.exe")
                                    if os.path.isfile(exe):
                                        cands.append(exe)
                            except OSError:
                                continue
                except OSError:
                    continue
        except ImportError:
            pass
    else:
        patterns = [
            "/usr/lib/jvm/*/bin/java",
            "/usr/lib/jvm/*/jre/bin/java",
            str(runtimes_dir() / "temurin-*" / "*" / "bin" / "java"),
            str(runtimes_dir() / "temurin-*" / "bin" / "java"),
        ]
        for pat in patterns:
            cands.extend(glob.glob(pat))

    # de-dupe, keep order
    seen, out = set(), []
    for c in cands:
        rp = os.path.normcase(os.path.abspath(c))
        if rp not in seen:
            seen.add(rp)
            out.append(c)
    return out


def find_javas() -> list[dict]:
    found = []
    for exe in _candidate_exes():
        info = probe_java(exe)
        if info:
            found.append(info)
    return found


def _cached_runtime(major: int) -> str | None:
    root = runtimes_dir()
    for exe_name in ("java.exe", "java"):
        for pat in (f"temurin-{major}/*/bin/{exe_name}", f"temurin-{major}/bin/{exe_name}"):
            for match in glob.glob(str(root / pat)):
                if probe_java(match):
                    return match
    return None


def _adoptium_url(major: int, image_type: str) -> str:
    system = {"Windows": "windows", "Darwin": "mac", "Linux": "linux"}[platform.system()]
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        arch = "x64"
    elif machine in ("arm64", "aarch64"):
        arch = "aarch64"
    else:
        arch = "x64"
    return (
        f"https://api.adoptium.net/v3/binary/latest/{major}/ga/"
        f"{system}/{arch}/{image_type}/hotspot/normal/eclipse"
    )


def _filename_from_headers(resp: requests.Response, fallback: str) -> str:
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r'filename="?([^";]+)"?', cd)
    if m:
        return m.group(1)
    ct = resp.headers.get("Content-Type", "")
    if "zip" in ct:
        return fallback + ".zip"
    return fallback + ".tar.gz"


def download_runtime(major: int, on_progress=None) -> str:
    """Download & extract a Temurin JRE/JDK for `major`. Returns java path."""
    cached = _cached_runtime(major)
    if cached:
        if on_progress:
            on_progress(f"Using cached Java {major}")
        return cached

    root = runtimes_dir()
    dest_dir = root / f"temurin-{major}"
    dest_dir.mkdir(parents=True, exist_ok=True)

    last_err: Exception | None = None
    for image_type in ("jre", "jdk"):
        url = _adoptium_url(major, image_type)
        try:
            with requests.get(
                url, stream=True, timeout=120,
                headers={"User-Agent": USER_AGENT}, allow_redirects=True,
            ) as resp:
                if resp.status_code >= 400:
                    raise RuntimeError(f"Adoptium returned HTTP {resp.status_code}")
                fname = _filename_from_headers(resp, f"temurin-{major}")
                archive = dest_dir / fname
                total = int(resp.headers.get("Content-Length", "0"))
                done = 0
                last_pct = -5
                with open(archive, "wb") as fh:
                    for chunk in resp.iter_content(1 << 16):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        done += len(chunk)
                        if on_progress and total:
                            pct = done * 100 // total
                            if pct >= last_pct + 5 or pct == 100:
                                last_pct = pct
                                on_progress(f"Downloading Java {major} ({image_type.upper()}): {pct}%")
            # extract
            if str(archive).endswith(".zip"):
                with zipfile.ZipFile(archive) as zf:
                    zf.extractall(dest_dir)
            else:
                with tarfile.open(archive) as tf:
                    tf.extractall(dest_dir, filter="data")
            try:
                os.remove(archive)
            except OSError:
                pass
            exe = _cached_runtime(major)
            if exe:
                return exe
            raise RuntimeError("extraction succeeded but java binary not found")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if on_progress:
                on_progress(f"Temurin {image_type.upper()} failed ({exc}); trying next option…")
    raise RuntimeError(f"Could not obtain Java {major}: {last_err}")


def ensure_java(required_major: int, on_progress=None) -> str:
    """Resolve a usable java executable for `required_major`."""
    cfg = load_config()
    override = (cfg.get("java_path") or "").strip()
    if override:
        info = probe_java(override)
        if info and info["major"] >= required_major:
            return override
        if on_progress:
            on_progress(
                f"Configured java_path is unusable/too old "
                f"(need >= {required_major}); falling back"
            )

    for info in find_javas():
        if info["major"] >= required_major:
            return info["path"]

    if on_progress:
        on_progress(f"No Java >= {required_major} found; downloading Temurin…")
    return download_runtime(required_major, on_progress)
