"""Flask backend for NP Launcher — serves the UI and a JSON API."""

import json
import os
import sys
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from . import __version__
from . import config as cfg
from . import auth_ms, import_ext
from . import instances, launch, loaders, mojang, modrinth, news, updater
from .java import find_javas
from .paths import data_dir, instances_dir, logs_dir, package_resource

DEFAULT_PORT = 5340


# --------------------------------------------------------------------------
# Background jobs (installs, launches) with polled progress
# --------------------------------------------------------------------------

class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"       # idle | running | done | error
        self.phase = ""
        self.done = 0
        self.total = 0
        self.message = ""
        self.result = None
        self.error = None

    def update(self, phase=None, done=None, total=None, message=None):
        with self.lock:
            if phase is not None:
                self.phase = str(phase)
            if done is not None:
                try:
                    self.done = int(done)
                except (TypeError, ValueError):
                    pass
            if total is not None:
                try:
                    self.total = int(total)
                except (TypeError, ValueError):
                    self.total = 1
            if message is not None:
                self.message = str(message)

    def snapshot(self):
        with self.lock:
            pct = 0
            if self.total:
                pct = min(100, int(self.done * 100 / self.total))
            return {
                "status": self.status,
                "phase": self.phase,
                "done": self.done,
                "total": self.total,
                "percent": pct,
                "message": self.message,
                "error": self.error,
                "result": self.result,
            }


JOB = Job()
JOB_LOCK = threading.Lock()


def _run_job(fn):
    with JOB_LOCK:
        if JOB.status == "running":
            return False
        JOB.status = "running"
        JOB.error = None
        JOB.result = None
        JOB.phase = ""
        JOB.message = ""
        JOB.done = 0
        JOB.total = 0

    def wrapper():
        try:
            JOB.result = fn()
            JOB.status = "done"
            JOB.update(message="Done")
        except Exception as exc:  # noqa: BLE001 — surfaced to the UI
            JOB.error = str(exc)
            JOB.status = "error"
            JOB.update(message=str(exc))

    threading.Thread(target=wrapper, daemon=True, name="np-job").start()
    return True


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------

def create_app() -> Flask:
    app = Flask(
        __name__,
        static_folder=package_resource("static"),
        static_url_path="/static",
        template_folder=package_resource("templates"),
    )
    app.config["JSON_AS_ASCII"] = False

    # ------------------------------------------------------------- pages
    @app.route("/")
    def index():
        return send_from_directory(package_resource("templates"), "index.html")

    @app.route("/api/version")
    def api_version():
        return jsonify({"version": __version__})

    @app.route("/api/uuid")
    def api_uuid():
        from .launch import offline_uuid

        name = (request.args.get("name") or "Player").strip() or "Player"
        return jsonify({"uuid": offline_uuid(name), "name": name})

    # ------------------------------------------------------- accounts
    @app.route("/api/accounts", methods=["GET"])
    def api_accounts_list():
        return jsonify(auth_ms.list_accounts())

    @app.route("/api/accounts/ms/start", methods=["POST"])
    def api_accounts_ms_start():
        return jsonify({"url": auth_ms.authorize_url()})

    @app.route("/api/accounts/ms/callback", methods=["POST"])
    def api_accounts_ms_callback():
        body = request.get_json(force=True, silent=True) or {}
        paste = (body.get("url") or "").strip()
        if not paste:
            return jsonify({"error": "paste the full URL from the final page"}), 400
        try:
            account = auth_ms.sign_in_with_paste(paste)
            return jsonify({"account": {"name": account["name"], "uuid": account["uuid"], "type": "microsoft"}})
        except auth_ms.AuthError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"sign-in failed: {exc}"}), 502

    @app.route("/api/accounts/select", methods=["POST"])
    def api_accounts_select():
        body = request.get_json(force=True, silent=True) or {}
        uuid = (body.get("uuid") or "").strip()
        ok = auth_ms.set_active(uuid)
        return jsonify({"selected": ok})

    @app.route("/api/accounts/<uuid>", methods=["DELETE"])
    def api_accounts_delete(uuid):
        return jsonify({"deleted": auth_ms.remove_account(uuid)})

    # --------------------------------------------------------- settings
    @app.route("/api/settings", methods=["GET"])
    def api_settings_get():
        return jsonify({"settings": cfg.load(), "data_dir": str(data_dir())})

    @app.route("/api/settings", methods=["PUT"])
    def api_settings_put():
        body = request.get_json(force=True, silent=True) or {}
        allowed = {k: body[k] for k in (
            "java_path", "offline_username", "default_min_mb", "default_max_mb",
            "jvm_args", "update_repo", "news_enabled", "discord_client_id",
        ) if k in body}
        return jsonify({"settings": cfg.save({**cfg.load(), **allowed})})

    @app.route("/api/java/detected")
    def api_java_detected():
        return jsonify({"javas": find_javas()})

    @app.route("/api/open_data_dir", methods=["POST"])
    def api_open_data_dir():
        path = data_dir()
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", str(path)])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(path)])
            return jsonify({"ok": True})
        except OSError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    # -------------------------------------------------------- versions
    @app.route("/api/versions")
    def api_versions():
        kind = request.args.get("type", "").strip()
        try:
            versions = mojang.list_versions()
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"could not fetch version manifest: {exc}"}), 502
        if kind:
            versions = [v for v in versions if v.get("type") == kind]
        return jsonify({"versions": versions})

    @app.route("/api/manifest/refresh", methods=["POST"])
    def api_manifest_refresh():
        try:
            mojang.get_manifest(force=True)
            return jsonify({"ok": True})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(exc)}), 502

    @app.route("/api/fabric/loaders")
    def api_fabric_loaders():
        try:
            return jsonify({"loaders": loaders.fabric_loaders()})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 502

    @app.route("/api/forge/versions")
    def api_forge_versions():
        mc = request.args.get("mc", "").strip()
        if not mc:
            return jsonify({"versions": []})
        try:
            return jsonify({"versions": loaders.forge_versions_for(mc)})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 502

    @app.route("/api/neoforge/versions")
    def api_neoforge_versions():
        try:
            return jsonify({"versions": loaders.neoforge_versions()})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 502

    # ------------------------------------------------------ instances
    @app.route("/api/instances", methods=["GET"])
    def api_instances_list():
        return jsonify({"instances": instances.list_instances()})

    @app.route("/api/instances", methods=["POST"])
    def api_instances_create():
        body = request.get_json(force=True, silent=True) or {}
        name = (body.get("name") or "").strip()
        version_id = (body.get("version_id") or "").strip()
        if not name or not version_id:
            return jsonify({"error": "name and version_id are required"}), 400
        inst = instances.create_instance(
            name=name,
            version_id=version_id,
            loader=body.get("loader", "vanilla"),
            loader_version=body.get("loader_version", ""),
            min_mb=int(body.get("min_mb", 1024)),
            max_mb=int(body.get("max_mb", 4096)),
            jvm_args=body.get("jvm_args", ""),
        )
        # Kick off loader installation in the background if needed.
        loader = inst["loader"]
        loader_version = inst.get("loader_version", "")
        if loader == "fabric" and loader_version:
            _run_job(lambda: _install_fabric_job(inst))
        elif loader == "forge" and loader_version:
            _run_job(lambda: _install_forge_job(inst))
        elif loader == "neoforge" and loader_version:
            _run_job(lambda: _install_neoforge_job(inst))
        return jsonify({"instance": inst}), 201

    def _install_fabric_job(inst):
        JOB.update(phase="fabric", message="Installing Fabric profile…")
        vid = loaders.install_fabric(inst["mc_version"], inst["loader_version"], log=lambda m: JOB.update(message=m))
        return instances.update_instance(inst["id"], {"version_id": vid})

    def _install_forge_job(inst):
        JOB.update(phase="forge", message="Running Forge installer…", total=1, done=0)
        vid = loaders.install_forge(inst["mc_version"], inst["loader_version"], log=lambda m: JOB.update(message=m))
        return instances.update_instance(inst["id"], {"version_id": vid})

    def _install_neoforge_job(inst):
        JOB.update(phase="neoforge", message="Running NeoForge installer…", total=1, done=0)
        vid = loaders.install_neoforge(inst["loader_version"], inst["mc_version"], log=lambda m: JOB.update(message=m))
        return instances.update_instance(inst["id"], {"version_id": vid})

    @app.route("/api/instances/<instance_id>", methods=["GET"])
    def api_instance_get(instance_id):
        inst = instances.get_instance(instance_id)
        if not inst:
            return jsonify({"error": "not found"}), 404
        return jsonify({"instance": inst})

    @app.route("/api/instances/<instance_id>", methods=["PUT"])
    def api_instance_put(instance_id):
        body = request.get_json(force=True, silent=True) or {}
        try:
            inst = instances.update_instance(instance_id, body)
            return jsonify({"instance": inst})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.route("/api/instances/<instance_id>", methods=["DELETE"])
    def api_instance_delete(instance_id):
        return jsonify({"deleted": instances.delete_instance(instance_id)})

    # -------------------------------------------------------- playing
    @app.route("/api/play", methods=["POST"])
    def api_play():
        body = request.get_json(force=True, silent=True) or {}
        inst = instances.get_instance(body.get("instance_id", ""))
        if not inst:
            return jsonify({"error": "instance not found"}), 404
        if launch.GAME.running:
            return jsonify({"error": "game is already running"}), 409

        def work():
            def cb(phase, done, total):
                # Two callback shapes flow through here:
                #   mojang.prepare_version -> (phase, done:int, total:int)
                #   java.ensure_java       -> ("java", message:str, 1)
                if isinstance(done, str):
                    JOB.update(phase=str(phase), message=done)
                    return
                try:
                    d = int(done) if done is not None else 0
                except (TypeError, ValueError):
                    d = 0
                try:
                    t = int(total) if total else 0
                except (TypeError, ValueError):
                    t = 0
                JOB.update(phase=str(phase), done=d, total=t,
                           message=f"{phase}: {d}/{t}" if t > 1 else str(phase))
            return launch.launch_instance(inst, on_progress=cb)

        ok = _run_job(work)
        return jsonify({"started": ok})

    @app.route("/api/stop", methods=["POST"])
    def api_stop():
        return jsonify({"stopped": launch.GAME.stop()})

    @app.route("/api/game")
    def api_game():
        snap = launch.GAME.snapshot()
        return jsonify(snap)

    @app.route("/api/job")
    def api_job():
        return jsonify({"job": JOB.snapshot(), "game": launch.GAME.snapshot()})

    # ------------------------------------------------------ modrinth
    @app.route("/api/modrinth/search")
    def api_modrinth_search():
        q = request.args.get("q", "")
        ptype = request.args.get("type", "mod")
        loader = request.args.get("loader", "")
        game_version = request.args.get("game_version", "")
        limit = int(request.args.get("limit", 24))
        offset = int(request.args.get("offset", 0))
        try:
            return jsonify(modrinth.search(q, ptype, loader, game_version, limit, offset))
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 502

    @app.route("/api/modrinth/install_mod", methods=["POST"])
    def api_modrinth_install_mod():
        body = request.get_json(force=True, silent=True) or {}
        inst = instances.get_instance(body.get("instance_id", ""))
        if not inst:
            return jsonify({"error": "instance not found"}), 404
        project_id = body.get("project_id", "")
        if not project_id:
            return jsonify({"error": "project_id required"}), 400

        def work():
            return {"path": modrinth.install_mod(inst, project_id, log=lambda m: JOB.update(message=m))}

        ok = _run_job(work)
        return jsonify({"started": ok})

    @app.route("/api/modrinth/install_pack", methods=["POST"])
    def api_modrinth_install_pack():
        body = request.get_json(force=True, silent=True) or {}
        project_id = body.get("project_id", "")
        if not project_id:
            return jsonify({"error": "project_id required"}), 400
        game_version = body.get("game_version", "")
        loader = body.get("loader", "")
        name = body.get("name", "")

        def work():
            JOB.update(phase="modpack", message="Resolving modpack…")
            f = modrinth.latest_modpack_file(project_id, game_version, loader)
            tmp = Path(logs_dir()).parent / "cache" / f["filename"]
            tmp.parent.mkdir(parents=True, exist_ok=True)
            from .download import download_file
            hashes = f.get("hashes", {})
            JOB.update(phase="modpack", message=f"Downloading {f['filename']}…")
            download_file(f["url"], str(tmp), sha1=hashes.get("sha1"), sha512=hashes.get("sha512"),
                          expected_size=f.get("size"), timeout=1800)
            inst = modrinth.install_mrpack_file(str(tmp), log=lambda m: JOB.update(message=m),
                                                 name_override=name or None)
            return {"instance": inst}

        ok = _run_job(work)
        return jsonify({"started": ok})

    @app.route("/api/modrinth/import_mrpack", methods=["POST"])
    def api_modrinth_import_mrpack():
        body = request.get_json(force=True, silent=True) or {}
        path = (body.get("path") or "").strip()
        if not path:
            return jsonify({"error": "path required (absolute path to a .mrpack file)"}), 400

        def work():
            inst = modrinth.install_mrpack_file(path, log=lambda m: JOB.update(message=m))
            return {"instance": inst}

        ok = _run_job(work)
        return jsonify({"started": ok})

    # -------------------------------------------------------- import
    @app.route("/api/import/scan")
    def api_import_scan():
        try:
            return jsonify({"sources": import_ext.scan_all()})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/import", methods=["POST"])
    def api_import():
        body = request.get_json(force=True, silent=True) or {}
        items = body.get("items") or []
        options = body.get("options") or {}
        if not items:
            return jsonify({"error": "nothing selected"}), 400

        def work():
            results = []
            total = len(items)
            for i, item in enumerate(items):
                label = item.get("dest_name") or item.get("source_id") or f"item {i+1}"
                JOB.update(phase="import", done=i, total=total, message=f"Importing {label}…")
                try:
                    r = import_ext.import_instance(
                        item.get("launcher", ""),
                        item.get("source_id", ""),
                        dest_name=item.get("dest_name") or "",
                        options=options,
                        log=lambda m: JOB.update(message=m),
                    )
                    results.append({"item": label, "ok": True, "instance": r["instance"]["id"]})
                except Exception as exc:  # noqa: BLE001
                    results.append({"item": label, "ok": False, "error": str(exc)})
            JOB.update(done=total, total=total)
            return {"imported": results}

        ok = _run_job(work)
        return jsonify({"started": ok})

    # ---------------------------------------------------- screenshots
    def _screenshots_dir(instance_id: str):
        inst = instances.get_instance(instance_id)
        if not inst:
            return None
        game_dir = instances_dir() / inst["id"]
        shots = game_dir / "screenshots"
        return shots if shots.is_dir() else None

    @app.route("/api/instances/<instance_id>/screenshots")
    def api_screenshots(instance_id):
        shots = _screenshots_dir(instance_id)
        if shots is None:
            return jsonify({"screenshots": []})
        items = []
        for p in shots.iterdir():
            if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                st = p.stat()
                items.append({"name": p.name, "size": st.st_size, "mtime": int(st.st_mtime)})
        items.sort(key=lambda s: s["mtime"], reverse=True)
        return jsonify({"screenshots": items})

    @app.route("/api/instances/<instance_id>/screenshots/<path:name>")
    def api_screenshot_file(instance_id, name):
        from flask import send_from_directory

        shots = _screenshots_dir(instance_id)
        if shots is None:
            return jsonify({"error": "not found"}), 404
        safe = os.path.basename(name)
        if safe != name:
            return jsonify({"error": "bad name"}), 400
        return send_from_directory(str(shots), safe)

    # ----------------------------------------------------------- news
    @app.route("/api/news")
    def api_news():
        if not cfg.get("news_enabled", True):
            return jsonify({"entries": [], "disabled": True})
        return jsonify({"entries": news.get_news(12)})

    # ---------------------------------------------------------- update
    @app.route("/api/update/check")
    def api_update_check():
        return jsonify(updater.check_for_update())

    @app.errorhandler(Exception)
    def _unhandled(exc):
        return jsonify({"error": str(exc)}), 500

    return app


def _pick_port(preferred: int) -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="NP Launcher")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    app = create_app()
    port = _pick_port(args.port)
    url = f"http://{args.host}:{port}/"
    print(f"\n  NP Launcher v{__version__}\n  Data dir: {data_dir()}\n  Listening on {url}\n")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=args.host, port=port, threaded=True, use_reloader=False)
