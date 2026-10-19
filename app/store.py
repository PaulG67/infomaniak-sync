"""SQLite-Zustand und kDrive-Zugangsdaten unter /config."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.engine import (
    RCLONE_EXAMPLE_REMOTE,
    parse_rclone_remotes,
    read_rclone_section,
    render_rclone_conf,
    render_remote_conf,
    validate_identity,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, config_dir: Path, external_config: Path | None = None):
        self.config_dir = config_dir
        self.external_config = external_config or Path("/rclone/rclone.conf")
        self.config_dir.mkdir(parents=True, exist_ok=True)
        for name in ("logs", "filters", "bisync", "versions"):
            (self.config_dir / name).mkdir(parents=True, exist_ok=True)
        self.db_path = self.config_dir / "app.db"
        self.conf_path = self.config_dir / "rclone.conf"
        self.active_conf = self.config_dir / "rclone-active.conf"
        self.password_path = self.config_dir / "kdrive.password"
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _db(self):
        store = self

        class _Ctx:
            def __enter__(self_inner):
                self_inner.conn = store._connect()
                return self_inner.conn

            def __exit__(self_inner, exc_type, exc, tb):
                if exc_type:
                    self_inner.conn.rollback()
                else:
                    self_inner.conn.commit()
                self_inner.conn.close()

        return _Ctx()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    job_name TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    dry_run INTEGER NOT NULL,
                    resync INTEGER NOT NULL,
                    log_path TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT ''
                )
                """
            )

    def get(self, key: str, default: str = "") -> str:
        with self._lock, self._db() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        with self._lock, self._db() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def settings(self) -> dict:
        paused = self.get("schedules_paused", "0") == "1"
        try:
            parallel = int(self.get("max_parallel", "1") or "1")
        except ValueError:
            parallel = 1
        parallel = min(3, max(1, parallel))
        return {
            "schedules_paused": paused,
            "max_parallel": parallel,
            "synology_share": self.get("synology_share", ""),
        }

    def update_settings(
        self,
        *,
        schedules_paused: bool | None = None,
        max_parallel: int | None = None,
        synology_share: str | None = None,
    ) -> dict:
        if schedules_paused is not None:
            self.set("schedules_paused", "1" if schedules_paused else "0")
        if max_parallel is not None:
            self.set("max_parallel", str(min(3, max(1, int(max_parallel)))))
        if synology_share is not None:
            self.set("synology_share", str(synology_share).strip())
        return self.settings()

    def password(self) -> str:
        if not self.password_path.is_file():
            return ""
        return self.password_path.read_text(encoding="utf-8")

    def password_set(self) -> bool:
        return bool(self.password())

    def pass_is_obscured(self) -> bool:
        return self.get("kdrive_pass_obscured") == "1"

    def _write_password(self, password: str) -> None:
        tmp = self.password_path.with_suffix(".tmp")
        tmp.write_text(password, encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(self.password_path)
        try:
            self.password_path.chmod(0o600)
        except OSError:
            pass

    def webdav_entries(self) -> list[dict]:
        path = self.external_config
        if not path.is_file():
            return []
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:1_000_000]
        except OSError:
            return []
        return [item for item in parse_rclone_remotes(text) if item.get("type", "").lower() == "webdav"]

    def backend(self) -> dict:
        mode = self.get("connection_mode", "")
        if not mode and (self.password_set() or self.get("kdrive_id")):
            mode = "manual"
        if mode == "rclone":
            name = self.get("rclone_remote", "")
            match = next((item for item in self.webdav_entries() if item["name"] == name), None)
            ready = False
            if match:
                try:
                    self.ensure_remote(name)
                    ready = self.active_conf.is_file()
                except (ValueError, OSError):
                    ready = False
            return {
                "mode": "rclone",
                "config_path": self.active_conf,
                "remote": name if ready else "",
                "pass_env": False,
                "ready": ready,
                "url": match["url"] if match else "",
                "user": match["user"] if match else "",
            }
        drive_id = self.get("kdrive_id", "")
        manual = mode == "manual" or bool(drive_id) or self.password_set()
        return {
            "mode": "manual" if manual else "",
            "config_path": self.conf_path,
            "remote": "kdrive",
            "pass_env": not self.pass_is_obscured(),
            "ready": bool(drive_id) and self.password_set() and self.conf_path.is_file(),
            "url": f"https://{drive_id}.connect.kdrive.infomaniak.com" if drive_id else "",
            "user": self.get("kdrive_user", ""),
        }

    def require_backend(self) -> dict:
        backend = self.backend()
        if backend["ready"]:
            return backend
        if backend["mode"] == "rclone":
            if not self.external_config.is_file():
                raise ValueError(
                    "rclone-Datei ist nicht eingebunden. Beispiel: "
                    "/boot/config/plugins/rclone/.rclone.conf mit dem Eintrag [Infomaniak]"
                )
            chosen = self.get("rclone_remote", "") or RCLONE_EXAMPLE_REMOTE
            raise ValueError(f"WebDAV-Eintrag {chosen} steht nicht in der rclone-Datei")
        raise ValueError("kDrive-Verbindung ist nicht eingerichtet")

    def rclone_env(self) -> dict:
        env = os.environ.copy()
        backend = self.backend()
        if backend["pass_env"] and backend["ready"]:
            env["RCLONE_CONFIG_KDRIVE_PASS"] = self.password()
        else:
            env.pop("RCLONE_CONFIG_KDRIVE_PASS", None)
        return env

    def _write_conf(self, path: Path, text: str) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(path)
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def ensure_remote(self, name: str) -> None:
        text = self.external_config.read_text(encoding="utf-8", errors="replace")[:1_000_000]
        digest = hashlib.sha256(f"{name}\n{text}".encode()).hexdigest()[:16]
        if self.get("active_digest") == digest and self.get("active_remote") == name and self.active_conf.is_file():
            return
        section = read_rclone_section(text, name) or {}
        if section.get("type", "").lower() != "webdav":
            raise ValueError("WebDAV-Eintrag nicht gefunden")
        secret = section.get("pass") or section.get("password") or ""
        body = render_remote_conf(
            name,
            section.get("url", ""),
            section.get("user", ""),
            secret,
            section.get("vendor") or "other",
        )
        self._write_conf(self.active_conf, body)
        self.set("active_digest", digest)
        self.set("active_remote", name)

    def use_rclone_remote(self, name: str) -> None:
        name = str(name or "").strip()
        if not self.external_config.is_file():
            raise ValueError(
                "rclone-Datei ist nicht eingebunden. Beispiel: "
                "/boot/config/plugins/rclone/.rclone.conf mit dem Eintrag [Infomaniak]"
            )
        names = [item["name"] for item in self.webdav_entries()]
        if not names:
            raise ValueError("Die rclone-Datei enthält keinen WebDAV-Eintrag")
        if name not in names:
            raise ValueError("WebDAV-Eintrag nicht gefunden")
        self.ensure_remote(name)
        self.set("connection_mode", "rclone")
        self.set("rclone_remote", name)

    def write_webdav(self, drive_id: str, user: str, password: str | None, *, obscured: bool = False) -> None:
        drive_id, user = validate_identity(drive_id, user, password if password is not None else None)
        if password is not None and password != "":
            validate_identity(drive_id, user, password)
            self._write_password(password)
        elif not self.password_set():
            raise ValueError("Passwort fehlt. Trag das Anwendungspasswort oder den pass-Wert aus rclone ein.")
        self.set("kdrive_pass_obscured", "1" if obscured else "0")
        secret = self.password() if obscured else None
        text = render_rclone_conf(drive_id, user, secret)
        self._write_conf(self.conf_path, text)
        self.set("kdrive_id", drive_id)
        self.set("kdrive_user", user)

    def connection_public(self) -> dict:
        backend = self.backend()
        drive_id = self.get("kdrive_id", "")
        user = self.get("kdrive_user", "")
        return {
            "mode": backend["mode"],
            "ready": backend["ready"],
            "rclone_remote": self.get("rclone_remote", ""),
            "rclone_present": self.external_config.is_file(),
            "example_path": "/boot/config/plugins/rclone/.rclone.conf",
            "example_remote": RCLONE_EXAMPLE_REMOTE,
            "remotes": self.webdav_entries(),
            "kdrive_id": drive_id,
            "kdrive_user": user,
            "password_set": self.password_set(),
            "password_obscured": self.pass_is_obscured(),
            "url": backend["url"],
            "user": backend["user"],
            "source": self.get("connection_source", ""),
        }

    def apply_env(self, drive_id: str, user: str, password: str, rclone_remote: str = "", *, force: bool = False) -> None:
        if self.get("connection_source") == "ui" and not force:
            return
        if self.external_config.is_file():
            names = [item["name"] for item in self.webdav_entries()]
            chosen = (rclone_remote or "").strip() or RCLONE_EXAMPLE_REMOTE
            self.set("connection_mode", "rclone")
            self.set("rclone_remote", chosen if chosen in names else "")
            self.set("connection_source", "env")
            if chosen in names:
                self.ensure_remote(chosen)
            return
        self.set("connection_mode", "manual")
        drive_id = (drive_id or "").strip()
        user = (user or "").strip()
        password = password or ""
        if not (drive_id or user or password):
            return
        current_id = self.get("kdrive_id", "")
        current_user = self.get("kdrive_user", "")
        new_id = drive_id or current_id
        new_user = user or current_user
        if not new_id or not new_user or (not password and not self.password_set()):
            if drive_id:
                validate_identity(drive_id, new_user or "placeholder@invalid.example", None)
                self.set("kdrive_id", drive_id)
            if user:
                validate_identity(new_id or "1", user, None)
                self.set("kdrive_user", user)
            self.set("connection_source", "env")
            return
        self.write_webdav(new_id, new_user, password or None)
        self.set("connection_source", "env")

    def list_jobs(self) -> list[dict]:
        with self._lock, self._db() as conn:
            rows = conn.execute("SELECT data FROM jobs ORDER BY updated_at").fetchall()
        jobs = [json.loads(row["data"]) for row in rows]
        jobs.sort(key=lambda job: job.get("name", "").lower())
        return jobs

    def get_job(self, job_id: str) -> dict | None:
        with self._lock, self._db() as conn:
            row = conn.execute("SELECT data FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def put_job(self, job: dict) -> None:
        with self._lock, self._db() as conn:
            conn.execute(
                """
                INSERT INTO jobs(id, data, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at
                """,
                (job["id"], json.dumps(job, ensure_ascii=False), job.get("updated_at") or utc_now()),
            )

    def update_job(self, job_id: str, mutator) -> dict | None:
        with self._lock:
            job = self.get_job(job_id)
            if not job:
                return None
            mutator(job)
            job["updated_at"] = utc_now()
            self.put_job(job)
            return job

    def delete_job(self, job_id: str) -> None:
        with self._lock, self._db() as conn:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            conn.execute("DELETE FROM runs WHERE job_id = ?", (job_id,))

    def create_run(self, run: dict) -> None:
        with self._lock, self._db() as conn:
            conn.execute(
                """
                INSERT INTO runs(id, job_id, job_name, started_at, finished_at, status, trigger, dry_run, resync, log_path, summary)
                VALUES(:id, :job_id, :job_name, :started_at, :finished_at, :status, :trigger, :dry_run, :resync, :log_path, :summary)
                """,
                run,
            )

    def finish_run(self, run_id: str, *, status: str, summary: str) -> None:
        with self._lock, self._db() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, summary = ?, finished_at = ? WHERE id = ?",
                (status, summary[:500], utc_now(), run_id),
            )

    def get_run(self, run_id: str) -> dict | None:
        with self._lock, self._db() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 50, job_id: str | None = None) -> list[dict]:
        limit = min(200, max(1, limit))
        with self._lock, self._db() as conn:
            if job_id:
                rows = conn.execute(
                    "SELECT * FROM runs WHERE job_id = ? ORDER BY started_at DESC LIMIT ?",
                    (job_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def interrupt_stale_runs(self) -> None:
        now = utc_now()
        with self._lock, self._db() as conn:
            conn.execute(
                "UPDATE runs SET status = 'interrupted', finished_at = ? WHERE status = 'running'",
                (now,),
            )
        for job in self.list_jobs():
            if job.get("last_status") == "running":
                self.update_job(job["id"], lambda item: item.__setitem__("last_status", "interrupted"))
