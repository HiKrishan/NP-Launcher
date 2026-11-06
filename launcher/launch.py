"""Build the JVM command and run the game, streaming logs."""

import hashlib
import os
import subprocess
import threading
import uuid
from collections import deque
from pathlib import Path

from . import auth_ms
from .config import load as load_config
from .java import ensure_java
from .mojang import (
    extract_natives,
    load_version,
    prepare_version,
    required_java_major,
    resolve_inheritance,
    _os_name,
)
from .instances import instance_game_dir, mark_played
from .paths import logs_dir

try:
    from . import discord_rpc
except Exception:  # pragma: no cover — never let presence break launching
    discord_rpc = None

PRESENCE = None


def _presence_client_id() -> str:
    return (load_config().get("discord_client_id") or "").strip()


def _start_presence(inst: dict, version_id: str) -> None:
    global PRESENCE
    client_id = _presence_client_id()
    if not client_id or discord_rpc is None:
        return
    try:
        p = discord_rpc.Presence(client_id)
        p.start(
            "Playing Minecraft",
            f"{inst.get('name', 'instance')} · {inst.get('mc_version') or version_id} · {inst.get('loader', 'vanilla')}",
            asset="minecraft",
            large_text=f"NP Launcher — {inst.get('name', 'instance')}",
        )
        PRESENCE = p
    except Exception:
        PRESENCE = None


def _stop_presence() -> None:
    global PRESENCE
    if PRESENCE is not None:
        try:
            PRESENCE.stop()
        except Exception:
            pass
        PRESENCE = None

MAX_LOG_LINES = 800


def offline_uuid(username: str) -> str:
    """Standard offline (cracked) UUID: name-based UUID of 'OfflinePlayer:<name>'."""
    digest = hashlib.md5(("OfflinePlayer:" + username).encode("utf-8")).digest()
    b = bytearray(digest[:16])
    b[6] = (b[6] & 0x0F) | 0x30  # version 3
    b[8] = (b[8] & 0x3F) | 0x80  # variant
    return str(uuid.UUID(bytes=bytes(b)))


def _sub_arg(arg, ctx: dict):
    if not isinstance(arg, str):
        return arg
    out = arg
    for key, value in ctx.items():
        out = out.replace("${" + key + "}", str(value))
    # strip any unresolved placeholders rather than pass garbage to the JVM
    if "${" in out:
        import re
        out = re.sub(r"\$\{[A-Za-z0-9_]+\}", "", out)
    return out


def _resolve_args(raw_args: list, ctx: dict) -> list:
    out = []
    for item in raw_args:
        if isinstance(item, dict):  # conditional rule object
            rules = item.get("rules", [])
            allowed = False
            for rule in rules:
                if _os_name() == (rule.get("os", {}) or {}).get("name", _os_name()):
                    allowed = rule.get("action") == "allow"
                elif not rule.get("os"):
                    allowed = rule.get("action") == "allow"
            if not allowed:
                continue
            value = item.get("value", [])
            out.extend(_resolve_args(value if isinstance(value, list) else [value], ctx))
        else:
            out.append(_sub_arg(item, ctx))
    return out


def build_command(inst: dict, data: dict, java_path: str, natives_path: Path, account: dict | None = None) -> list[str]:
    cfg = load_config()
    game_dir = instance_game_dir(inst)
    version_id = data.get("id", inst.get("version_id"))

    # classpath: libraries + client jar
    from .mojang import resolve_libraries

    libs, _natives = resolve_libraries(data)
    client_jar = Path(data.get("id", version_id))
    from .paths import versions_dir

    jar = versions_dir() / version_id / f"{version_id}.jar"
    classpath = libs + [str(jar)]
    sep = ";" if os.name == "nt" else ":"

    if account:
        mc_token = account.get("mc_token", "")
        username = account.get("name", "Player")
        player_uuid = account.get("uuid", offline_uuid(username))
        user_type = "msa"
    else:
        username = (cfg.get("offline_username") or "Player").strip() or "Player"
        player_uuid = offline_uuid(username)
        mc_token = "0"
        user_type = "legacy"
    ctx = {
        "auth_player_name": username,
        "auth_uuid": player_uuid,
        "auth_access_token": mc_token,
        "auth_session": f"token:{mc_token}:{player_uuid}",
        "clientid": "np-launcher",
        "auth_xuid": "0",
        "user_type": user_type,
        "user_properties": "{}",
        "version_name": version_id,
        "game_directory": str(game_dir),
        "assets_root": str(Path(data.get("_assets_root", "")) or (game_dir)),
        "assets_index_name": (data.get("assetIndex") or {}).get("id", "legacy"),
        "game_assets": str(game_dir),
        "version_type": data.get("type", "release"),
        "natives_directory": str(natives_path),
        "launcher_name": "NPLauncher",
        "launcher_version": "1.0.0",
        "classpath": sep.join(classpath),
        "classpath_separator": sep,
        "library_directory": str(versions_dir().parent / "libraries"),
        "classpath_separator": sep,
    }

    min_mb = int(inst.get("min_mb") or cfg.get("default_min_mb", 1024))
    max_mb = int(inst.get("max_mb") or cfg.get("default_max_mb", 4096))
    if max_mb < min_mb:
        min_mb = max_mb

    cmd = [java_path]
    cmd.append(f"-Xms{min_mb}m")
    cmd.append(f"-Xmx{max_mb}m")

    extra_jvm = (inst.get("jvm_args") or "").strip() or (cfg.get("jvm_args") or "").strip()
    if extra_jvm:
        cmd.extend(extra_jvm.split())

    # modern (arguments.jvm) vs legacy (minecraftArguments-only) versions
    args = data.get("arguments") or {}
    jvm_raw = args.get("jvm")
    if jvm_raw is None:
        jvm_raw = [
            "-Djava.library.path=${natives_directory}",
            "-cp", "${classpath}",
        ]
    jvm_args = _resolve_args(list(jvm_raw), ctx)
    if not any("-cp" == str(a) or str(a).startswith("-cp=") for a in jvm_args):
        jvm_args += ["-cp", sep.join(classpath)]
    jvm_args = [a for a in jvm_args if a != "${classpath}"]
    cmd.extend(jvm_args)
    cmd.append(data.get("mainClass", "net.minecraft.client.main.Main"))

    game_raw = args.get("game")
    if game_raw is None:
        legacy = data.get("minecraftArguments", "")
        game_args = [a for a in legacy.split(" ") if a]
        game_args = [_sub_arg(a, ctx) for a in game_args]
    else:
        game_args = _resolve_args(list(game_raw), ctx)
    cmd.extend(game_args)
    return cmd


class GameProcess:
    """Wraps the running game: pid, log ring buffer, stop()."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.logs: deque[str] = deque(maxlen=MAX_LOG_LINES)
        self.lock = threading.Lock()
        self.running = False
        self.exit_code: int | None = None

    def start(self, cmd: list[str], cwd: Path, log_path: Path) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_file = open(log_path, "a", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
        )
        self.running = True
        self.exit_code = None

        def reader():
            assert self.proc is not None
            for line in self.proc.stdout:  # type: ignore[union-attr]
                line = line.rstrip("\n")
                with self.lock:
                    self.logs.append(line)
                try:
                    self._log_file.write(line + "\n")
                    self._log_file.flush()
                except OSError:
                    pass
            self.exit_code = self.proc.wait()
            self.running = False
            try:
                self._log_file.close()
            except OSError:
                pass

        threading.Thread(target=reader, daemon=True, name="game-log").start()

    def stop(self) -> bool:
        if self.proc and self.running:
            try:
                self.proc.terminate()
                return True
            except OSError:
                return False
        return False

    def snapshot(self) -> dict:
        with self.lock:
            lines = list(self.logs)
        return {"running": self.running, "exit_code": self.exit_code, "lines": lines}


GAME = GameProcess()


def launch_instance(inst: dict, on_progress=None) -> dict:
    """Full pipeline: prepare files, build command, spawn process."""
    if GAME.running:
        raise RuntimeError("the game is already running")

    version_id = inst.get("version_id") or inst.get("mc_version")
    if not version_id:
        raise ValueError("instance has no version configured")

    def cb(phase, done, total):
        if on_progress:
            on_progress(phase, done, total)

    data = prepare_version(version_id, on_progress=cb)

    from .mojang import resolve_libraries
    from .paths import assets_dir
    data["_assets_root"] = str(assets_dir())

    required = required_java_major(data)
    java_path = ensure_java(required, on_progress=lambda msg: on_progress and on_progress("java", msg, 1))

    classpath_natives = extract_natives(
        resolve_libraries(data)[1],
        version_id,
    )
    game_dir = instance_game_dir(inst)
    game_dir.mkdir(parents=True, exist_ok=True)

    account = None
    try:
        account = auth_ms.active_account_for_launch()
    except auth_ms.AuthError as exc:
        raise RuntimeError(f"Microsoft sign-in problem: {exc}") from exc
    if account:
        if on_progress:
            on_progress("auth", None, None)
        print(f"[np-launcher] launching as Microsoft account: {account.get('name')}")

    cmd = build_command(inst, data, java_path, classpath_natives, account)
    log_path = logs_dir() / f"{inst['id']}.log"
    GAME.start(cmd, game_dir, log_path)
    _start_presence(inst, version_id)

    def _watch_presence():
        import time as _time

        while GAME.running:
            _time.sleep(2)
        _stop_presence()

    threading.Thread(target=_watch_presence, daemon=True, name="presence-watch").start()
    mark_played(inst["id"])
    return {"launched": True, "pid": GAME.proc.pid if GAME.proc else None, "command_preview": cmd[:8]}
