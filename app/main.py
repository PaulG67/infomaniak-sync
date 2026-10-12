"""Weboberfläche für die kDrive-Synchronisation."""

from __future__ import annotations

import hmac
import logging
import os
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, abort, jsonify, redirect, render_template, request, session
from werkzeug.exceptions import HTTPException

from app import engine
from app.runner import Runner, directory_names, probe
from app.scheduler import start_scheduler
from app.store import Store

log = logging.getLogger("app")


def load_tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "Europe/Zurich")
    except Exception:
        return ZoneInfo("UTC")


def rclone_version() -> str | None:
    try:
        proc = subprocess.run(
            ["rclone", "version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = (proc.stdout or "").splitlines()
    return lines[0].strip() if lines else None


def share_count(root: Path) -> int:
    if not root.is_dir():
        return 0
    total = 0
    try:
        for entry in root.iterdir():
            if entry.name.startswith(".") or entry.is_symlink():
                continue
            if entry.is_dir():
                total += 1
    except OSError:
        return 0
    return total


def create_app() -> Flask:
    config_dir = Path(os.environ.get("CONFIG_DIR", "/config"))
    shares_root = Path(os.environ.get("SHARES_ROOT", "/shares"))
    tz = load_tz(os.environ.get("TZ", "Europe/Zurich"))
    web_password = os.environ.get("WEB_PASSWORD") or ""
    web_user = os.environ.get("WEB_USER") or "admin"

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    store = Store(config_dir)
    env_warning = ""
    try:
        store.apply_env(
            os.environ.get("KDRIVE_ID", ""),
            os.environ.get("KDRIVE_USER", ""),
            os.environ.get("KDRIVE_PASSWORD", ""),
        )
    except ValueError as exc:
        env_warning = str(exc)
        log.warning("Container-Parameter ignoriert: %s", exc)
    store.interrupt_stale_runs()
    runner = Runner(store, shares_root, tz)
    if os.environ.get("SYNC_SCHEDULER", "1") != "0":
        start_scheduler(runner)

    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False
    app.json.ensure_ascii = False
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
    app.config["PROPAGATE_EXCEPTIONS"] = False
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
    secret_path = config_dir / ".flask_secret"
    if not secret_path.exists():
        secret_path.write_bytes(os.urandom(32))
        try:
            secret_path.chmod(0o600)
        except OSError:
            pass
    app.secret_key = secret_path.read_bytes()
    app.config["STORE"] = store
    app.config["RUNNER"] = runner
    app.config["SHARES"] = shares_root
    app.config["TZ"] = tz
    app.config["TZ_NAME"] = str(tz)
    app.config["WEB_PASSWORD"] = web_password
    app.config["WEB_USER"] = web_user
    app.config["ENV_WARNING"] = env_warning
    app.config["RCLONE"] = rclone_version()

    def now() -> datetime:
        return datetime.now(tz)

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.before_request
    def auth_gate():
        if not app.config["WEB_PASSWORD"]:
            return None
        if request.path in {"/login", "/health"} or request.path.startswith("/static/"):
            return None
        if session.get("auth") == "ok":
            return None
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "Anmeldung erforderlich"}), 401
        return redirect("/login")

    @app.errorhandler(HTTPException)
    def http_error(exc: HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": exc.description}), exc.code
        return exc

    def public_runs(limit: int = 30) -> list[dict]:
        rows = []
        for run in store.list_runs(limit):
            rows.append(
                {
                    "id": run["id"],
                    "job_id": run["job_id"],
                    "job_name": run["job_name"],
                    "started_at": run["started_at"],
                    "finished_at": run["finished_at"],
                    "status": run["status"],
                    "trigger": run["trigger"],
                    "dry_run": bool(run["dry_run"]),
                    "resync": bool(run["resync"]),
                    "summary": run["summary"],
                }
            )
        return rows

    def state() -> dict:
        jobs = [engine.public_job(job) for job in store.list_jobs()]
        connection = store.connection_public()
        env_ready = all(
            os.environ.get(key, "").strip() for key in ("KDRIVE_ID", "KDRIVE_USER", "KDRIVE_PASSWORD")
        )
        return {
            "ok": True,
            "now": now().isoformat(),
            "tz": app.config["TZ_NAME"],
            "shares_root": str(shares_root),
            "shares_ok": shares_root.is_dir(),
            "share_count": share_count(shares_root),
            "rclone": app.config["RCLONE"],
            "env_warning": app.config["ENV_WARNING"],
            "env_ready": env_ready,
            "auth_required": bool(app.config["WEB_PASSWORD"]),
            "connection": connection,
            "settings": store.settings(),
            "jobs": jobs,
            "runs": public_runs(),
            "active": runner.active(),
            "blank": engine.blank_job(),
        }

    def payload() -> dict:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("JSON-Daten fehlen")
        return data

    def require_job(job_id: str) -> dict:
        job = store.get_job(job_id)
        if not job:
            abort(404, "Auftrag nicht gefunden")
        return job

    def save_from(raw: dict, old: dict | None) -> dict:
        job = engine.prepare_job(raw, old=old, shares_root=shares_root, now=now(), tz=tz)
        store.put_job(job)
        return engine.public_job(job)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/")
    def index():
        return render_template("index.html", auth_required=bool(web_password))

    @app.get("/login")
    def login_form():
        if not web_password:
            return redirect("/")
        return render_template("login.html", error="")

    @app.post("/login")
    def login_post():
        user = request.form.get("user") or ""
        password = request.form.get("password") or ""
        user_ok = hmac.compare_digest(user, web_user)
        pass_ok = hmac.compare_digest(password, web_password)
        if user_ok and pass_ok:
            session.clear()
            session["auth"] = "ok"
            session.permanent = True
            return redirect("/")
        return render_template("login.html", error="Anmeldung fehlgeschlagen"), 401

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect("/login" if web_password else "/")

    @app.get("/favicon.ico")
    def favicon():
        return redirect("/static/icon.svg")

    @app.get("/api/state")
    def api_state():
        return jsonify(state())

    @app.get("/api/local")
    def api_local():
        rel = engine.clean_rel(request.args.get("path", ""), label="Unraid-Ordner")
        folder = engine.local_path(shares_root, rel)
        dirs = []
        try:
            entries = sorted(folder.iterdir(), key=lambda item: item.name.lower())
        except OSError as exc:
            abort(403, f"Kein Zugriff: {exc.strerror or exc}")
        for entry in entries:
            if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
                continue
            child = rel + "/" + entry.name if rel else entry.name
            dirs.append({"name": entry.name, "path": child})
        return jsonify({"ok": True, "path": rel, "dirs": dirs})

    @app.get("/api/remote")
    def api_remote():
        rel = engine.clean_rel(request.args.get("path", ""), label="kDrive-Ordner")
        target = f"kdrive:{rel}" if rel else "kdrive:"
        code, text = probe(
            store,
            ["rclone", "lsf", target, "--dirs-only", "--max-depth", "1", "--config", str(store.conf_path), "--contimeout", "20s", "--timeout", "30s"],
        )
        if code != 0:
            raise ValueError(text or "kDrive-Ordner nicht lesbar")
        return jsonify({"ok": True, "path": rel, "dirs": [{"name": name, "path": f"{rel}/{name}".strip("/")} for name in directory_names(text)]})

    @app.post("/api/remote/mkdir")
    def api_mkdir():
        rel = engine.clean_rel(payload().get("path"), label="kDrive-Ordner")
        if not rel:
            raise ValueError("Ordnername fehlt")
        code, text = probe(
            store,
            ["rclone", "mkdir", f"kdrive:{rel}", "--config", str(store.conf_path)],
            timeout=40,
        )
        if code != 0:
            raise ValueError(text or "Ordner konnte nicht angelegt werden")
        return jsonify({"ok": True, "path": rel})

    @app.post("/api/connection")
    def api_connection():
        data = payload()
        password = data.get("password")
        if password is not None and str(password) == "":
            password = None
        store.write_webdav(str(data.get("kdrive_id") or ""), str(data.get("kdrive_user") or ""), password)
        store.set("connection_source", "ui")
        return jsonify({"ok": True, "connection": store.connection_public()})

    @app.post("/api/connection/test")
    def api_connection_test():
        code, text = probe(
            store,
            ["rclone", "lsf", "kdrive:", "--dirs-only", "--max-depth", "1", "--config", str(store.conf_path), "--contimeout", "20s", "--timeout", "35s"],
            timeout=40,
        )
        if code != 0:
            return jsonify({"ok": False, "error": text or "Verbindung fehlgeschlagen"}), 400
        return jsonify({"ok": True, "directories": directory_names(text), "message": "Verbindung steht"})

    @app.post("/api/connection/reset-env")
    def api_reset_env():
        drive_id = os.environ.get("KDRIVE_ID", "")
        user = os.environ.get("KDRIVE_USER", "")
        password = os.environ.get("KDRIVE_PASSWORD", "")
        if not (drive_id.strip() and user.strip() and password):
            raise ValueError("KDRIVE_ID, KDRIVE_USER und KDRIVE_PASSWORD sind im Container nicht gesetzt")
        store.write_webdav(drive_id, user, password)
        store.set("connection_source", "env")
        return jsonify({"ok": True, "connection": store.connection_public()})

    @app.post("/api/settings")
    def api_settings():
        data = payload()
        paused = data.get("schedules_paused")
        parallel = data.get("max_parallel")
        settings = store.update_settings(
            schedules_paused=None if paused is None else engine.as_bool(paused),
            max_parallel=None if parallel is None else int(parallel),
        )
        return jsonify({"ok": True, "settings": settings})

    @app.post("/api/preview")
    def api_preview():
        data = payload()
        raw = data.get("job") if isinstance(data.get("job"), dict) else data
        try:
            job = engine.prepare_job(raw, old=None, shares_root=shares_root, now=now(), tz=tz, relax_confirm=True)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc), "description": "", "commands": []}), 400
        if raw.get("id"):
            old = store.get_job(str(raw.get("id")))
            if old:
                job = engine.prepare_job(raw, old=old, shares_root=shares_root, now=now(), tz=tz, relax_confirm=True)
        commands = engine.build_commands(
            job,
            shares_root=shares_root,
            config_path=store.conf_path,
            filter_path=store.config_dir / "filters" / f"{job['id']}.txt",
            workdir_base=store.config_dir / "bisync",
            version_root=store.config_dir / "versions",
            dry_run=False,
            resync=engine.needs_resync(job),
            stamp="ZEITSTEMPEL",
        )
        return jsonify(
            {
                "ok": True,
                "description": engine.describe(job),
                "commands": [engine.format_command(argv) for argv in commands],
            }
        )

    @app.post("/api/jobs")
    def api_create_job():
        job = save_from(payload(), None)
        return jsonify({"ok": True, "job": job})

    @app.put("/api/jobs/<job_id>")
    def api_update_job(job_id: str):
        job = save_from(payload(), require_job(job_id))
        return jsonify({"ok": True, "job": job})

    @app.delete("/api/jobs/<job_id>")
    def api_delete_job(job_id: str):
        if runner.is_running(job_id):
            raise ValueError("Auftrag läuft noch")
        require_job(job_id)
        for run in store.list_runs(200, job_id):
            path = Path(run["log_path"])
            if path.is_file():
                path.unlink(missing_ok=True)
        shutil.rmtree(store.config_dir / "bisync" / job_id, ignore_errors=True)
        filter_path = store.config_dir / "filters" / f"{job_id}.txt"
        filter_path.unlink(missing_ok=True)
        store.delete_job(job_id)
        return jsonify({"ok": True})

    @app.post("/api/jobs/<job_id>/run")
    def api_run(job_id: str):
        data = request.get_json(silent=True) or {}
        run = runner.start(require_job(job_id), trigger="manual", dry_run=engine.as_bool(data.get("dry_run")))
        return jsonify({"ok": True, "run": {"id": run["id"], "status": run["status"]}})

    @app.post("/api/jobs/<job_id>/resync")
    def api_resync(job_id: str):
        job = require_job(job_id)
        if job["mode"] != "bisync":
            raise ValueError("Erstabgleich gibt es nur bei beidseitigem Abgleich")
        run = runner.start(job, trigger="manual", resync=True)
        return jsonify({"ok": True, "run": {"id": run["id"], "status": run["status"]}})

    @app.post("/api/jobs/<job_id>/stop")
    def api_stop(job_id: str):
        if not runner.stop(job_id):
            raise ValueError("Kein laufender Auftrag")
        return jsonify({"ok": True})

    @app.get("/api/runs/<run_id>/log")
    def api_log(run_id: str):
        run = store.get_run(run_id)
        if not run:
            abort(404, "Lauf nicht gefunden")
        path = Path(run["log_path"]).resolve()
        try:
            path.relative_to((store.config_dir / "logs").resolve())
        except ValueError:
            abort(400, "Protokollpfad ungültig")
        try:
            offset = max(0, int(request.args.get("offset", "0")))
        except ValueError:
            offset = 0
        if not path.is_file():
            return jsonify({"ok": True, "offset": 0, "text": "", "status": run["status"]})
        with path.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read(200_000)
        return jsonify(
            {
                "ok": True,
                "offset": offset + len(chunk),
                "text": chunk.decode("utf-8", "replace"),
                "status": run["status"],
            }
        )

    @app.errorhandler(ValueError)
    def value_error(exc: ValueError):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": str(exc)}), 400
        raise exc

    return app


app = create_app() if os.environ.get("CREATE_APP_ON_IMPORT", "1") == "1" else None
