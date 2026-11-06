"""Central path handling for NP Launcher.

User data (game files, libraries, assets, instances, settings) lives in a
dedicated data directory — never inside the install folder — so PyInstaller
builds and pip installs behave the same.
"""

import os
import sys
from pathlib import Path

_ENV_VAR = "NP_LAUNCHER_DATA"


def data_dir() -> Path:
    override = os.environ.get(_ENV_VAR)
    base = Path(override) if override else Path.home() / ".np-launcher"
    ensure_dirs(base)
    return base


def ensure_dirs(base: Path) -> None:
    for rel in (
        "libraries",
        "versions",
        "assets/objects",
        "assets/indexes",
        "assets/skins",
        "natives",
        "runtimes",
        "instances",
        "logs",
        "cache",
    ):
        (base / rel).mkdir(parents=True, exist_ok=True)


def libraries_dir(root: Path | None = None) -> Path:
    return (root or data_dir()) / "libraries"


def versions_dir(root: Path | None = None) -> Path:
    return (root or data_dir()) / "versions"


def assets_dir(root: Path | None = None) -> Path:
    return (root or data_dir()) / "assets"


def natives_dir(root: Path | None = None) -> Path:
    return (root or data_dir()) / "natives"


def instances_dir(root: Path | None = None) -> Path:
    return (root or data_dir()) / "instances"


def runtimes_dir(root: Path | None = None) -> Path:
    return (root or data_dir()) / "runtimes"


def cache_dir(root: Path | None = None) -> Path:
    return (root or data_dir()) / "cache"


def logs_dir(root: Path | None = None) -> Path:
    return (root or data_dir()) / "logs"


def package_resource(*parts: str) -> str:
    """Path to a read-only resource shipped with the package (templates/static)."""
    if getattr(sys, "frozen", False):  # PyInstaller bundle
        base = Path(sys._MEIPASS) / "launcher"  # type: ignore[attr-defined]
    else:
        base = Path(__file__).resolve().parent
    return str(base.joinpath(*parts))
