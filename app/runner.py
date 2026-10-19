"""rclone-Läufe und Zeitplan."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app import engine
from app.remotes import resolve_synology_root
from app.store import Store

log = logging.getLogger("app.runner")


class Runner:
    def __init__(
        self,
        store: Store,
        shares_root: Path,
        tz: ZoneInfo,
        synology_root: Path | None = None,
        synology_resolver=None,
    ):
        self.store = store
        self.shares_root = shares_root
        self._remotes_root = synology_root
        self._synology_resolver = synology_resolver
        self.tz = tz
        self._procs: dict[str, subprocess.Popen] = {}
        self._cancel: set[str] = set()
        self._lock = threading.Lock()

    @property
    def remotes_root(self) -> Path | None:
        return self._remotes_root

    @property
    def synology_root(self) -> Path | None:
        if self._synology_resolver is not None:
            return self._synology_resolver()
        return self._remotes_root

    def synology_for(self, job: dict) -> Path | None:
        """SMB-Ziel für einen Auftrag: Remotes/<synology_share>."""
        if not job.get("synology_enabled"):
            return None
        base = self._remotes_root
        if base is None or not base.is_dir():
            return None
        share = (job.get("synology_share") or "").strip() or self.store.get("synology_share", "")
        if not share:
            return None
        try:
            target = resolve_synology_root(base, share)
        except ValueError:
            return None
        return target if target.is_dir() else None

    def running_job_ids(self) -> set[str]:
        with self._lock:
            live = set()
            for job_id, proc in list(self._procs.items()):
                if proc.poll() is None:
                    live.add(job_id)
            return live

    def is_running(self, job_id: str) -> bool:
        return job_id in self.running_job_ids()

    def active(self) -> list[dict]:
        runs = [run for run in self.store.list_runs(limit=20) if run["status"] == "running"]
        return [
            {
                "id": run["id"],
                "job_id": run["job_id"],
                "job_name": run["job_name"],
                "started_at": run["started_at"],
                "dry_run": bool(run["dry_run"]),
                "resync": bool(run["resync"]),
                "trigger": run["trigger"],
            }
            for run in runs
        ]

    def start(self, job: dict, *, trigger: str, dry_run: bool = False, resync: bool = False) -> dict:
        if self.is_running(job["id"]):
            raise ValueError("Dieser Auftrag läuft bereits")
        if job["mode"] in engine.MIRROR_MODES and not job.get("confirm_delete"):
            raise ValueError("Löschen ist für diesen Auftrag nicht bestätigt")
        effective_resync = resync or (dry_run and engine.needs_resync(job))
        if engine.needs_resync(job) and not dry_run and not resync:
            raise ValueError("Beidseitig braucht zuerst einen Erstabgleich")
        if job.get("kdrive_enabled", True):
            self.store.require_backend()
        if job.get("synology_enabled") and self.synology_for(job) is None:
            if self._remotes_root is None or not self._remotes_root.is_dir():
                raise ValueError(
                    f"SMB-Remotes fehlen. Im Template den Path Remotes auf {engine.SYNOLOGY_EXAMPLE} setzen."
                )
            share = (job.get("synology_share") or "").strip()
            if not share:
                raise ValueError("SMB-Share für diesen Auftrag wählen")
            raise ValueError(f"SMB-Share «{share}» fehlt oder ist nicht eingehängt")
        now = datetime.now(self.tz)
        run_id = str(uuid.uuid4())
        log_path = self.store.config_dir / "logs" / f"{run_id}.log"
        snapshot = self.store.get_job(job["id"]) or job
        run = {
            "id": run_id,
            "job_id": snapshot["id"],
            "job_name": snapshot["name"],
            "started_at": now.isoformat(),
            "finished_at": None,
            "status": "running",
            "trigger": trigger,
            "dry_run": 1 if dry_run else 0,
            "resync": 1 if effective_resync else 0,
            "log_path": str(log_path),
            "summary": "",
        }
        self.store.create_run(run)
        self.store.update_job(
            snapshot["id"],
            lambda item: item.update(last_status="running", last_summary="läuft"),
        )
        thread = threading.Thread(
            target=self._execute,
            args=(run_id, snapshot, dry_run, effective_resync, log_path),
            name=f"sync-{snapshot['id'][:8]}",
            daemon=True,
        )
        thread.start()
        return run

    def stop(self, job_id: str) -> bool:
        self._cancel.add(job_id)
        with self._lock:
            proc = self._procs.get(job_id)
        if proc is not None and proc.poll() is None:
            self._terminate(proc)
            return True
        running = [item for item in self.store.list_runs(10, job_id) if item["status"] == "running"]
        if running:
            return True
        self._cancel.discard(job_id)
        return False

    def schedule_tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(self.tz)
        settings = self.store.settings()
        if settings["schedules_paused"]:
            return
        running = self.running_job_ids()
        slots = settings["max_parallel"] - len(running)
        if slots <= 0:
            return
        for job in self.store.list_jobs():
            if slots <= 0:
                return
            if not job.get("enabled") or job["id"] in running:
                continue
            if job.get("schedule", {}).get("type") == "manual":
                continue
            if engine.schedule_block(job):
                continue
            nxt = job.get("next_run")
            if not nxt:
                continue
            due = datetime.fromisoformat(nxt)
            if due.tzinfo is None:
                due = due.replace(tzinfo=self.tz)
            if due > engine.aware(now, self.tz):
                continue
            following = engine.next_occurrence(job["schedule"], now, self.tz)

            def claim(item, _following=following):
                item["next_run"] = _following.isoformat() if _following else None

            self.store.update_job(job["id"], claim)
            try:
                self.start(job, trigger="schedule")
            except ValueError as exc:
                log.info("Auftrag %s nicht gestartet: %s", job.get("name"), exc)
                continue
            slots -= 1
            running.add(job["id"])

    def _execute(self, run_id: str, job: dict, dry_run: bool, resync: bool, log_path: Path) -> None:
        status = "ok"
        summary = ""
        try:
            if job["id"] in self._cancel:
                status = "cancelled"
                summary = "abgebrochen"
                return
            extra = []
            nested = engine.config_exclude(self.store.config_dir, self.shares_root)
            if nested:
                extra.append(nested)
            filters = engine.render_filters(job, extra)
            filter_path = self.store.config_dir / "filters" / f"{job['id']}.txt"
            filter_path.write_text(filters, encoding="utf-8")
            stamp = datetime.now(self.tz).strftime("%Y%m%d-%H%M%S")
            if job["existing"] == "version" or (job["mode"] == "bisync" and resync):
                slug = engine.slugify(job["name"], job["id"])
                (self.store.config_dir / "versions" / slug / stamp).mkdir(parents=True, exist_ok=True)
            if job.get("kdrive_enabled", True):
                backend = self.store.require_backend()
                remote_name = backend["remote"]
                config_path = backend["config_path"]
            else:
                backend = self.store.backend()
                remote_name = backend.get("remote") or "kdrive"
                config_path = backend.get("config_path") or self.store.conf_path
                if not Path(config_path).is_file():
                    config_path = self.store.config_dir / "rclone-local.conf"
                    if not config_path.is_file():
                        config_path.write_text("# local only\n", encoding="utf-8")
            steps = engine.build_commands(
                job,
                shares_root=self.shares_root,
                config_path=Path(config_path),
                filter_path=filter_path,
                workdir_base=self.store.config_dir / "bisync",
                version_root=self.store.config_dir / "versions",
                dry_run=dry_run,
                resync=resync,
                stamp=stamp,
                remote_name=remote_name,
                synology_root=self.synology_for(job),
            )
            with log_path.open("w", encoding="utf-8") as handle:
                handle.write(f"# {job['name']}\n")
                handle.write(f"# Start {datetime.now(self.tz).isoformat()}\n")
                if dry_run:
                    handle.write("# Probelauf, es wird nichts verändert\n")
                if resync:
                    handle.write("# Erstabgleich (rclone resync). Überschriebene Fassungen landen im Versionsordner.\n")
                for step in steps:
                    handle.write(f"# [{step['target']}] " + engine.format_command(step["argv"]) + "\n")
                handle.write("\n")
            env = self.store.rclone_env()
            for index, step in enumerate(steps):
                argv = list(step["argv"])
                if dry_run:
                    with log_path.open("a", encoding="utf-8") as handle:
                        handle.write("# PLAN " + json.dumps(step["plan"], ensure_ascii=False) + "\n")
                combined_path = None
                if dry_run and job["mode"] != "bisync":
                    combined_path = log_path.with_name(f"{log_path.stem}-{index}.combined")
                    combined_path.unlink(missing_ok=True)
                    argv = [*argv, "--combined", str(combined_path)]
                if job["id"] in self._cancel:
                    status = "cancelled"
                    break
                code = self._run_process(job["id"], argv, log_path, env)
                self._append_combined(log_path, combined_path)
                if job["id"] in self._cancel or code < 0:
                    status = "cancelled"
                    break
                if code != 0:
                    status = "error"
                    break
            if status == "ok" and resync and not dry_run and job.get("kdrive_enabled", True):
                pairs = {(m["local"], m["remote"]) for m in job["mappings"]}

                def mark(item, _pairs=pairs):
                    if item.get("mode") != "bisync":
                        return
                    if item.get("option_signature") != job.get("option_signature"):
                        return
                    for mapping in item.get("mappings") or []:
                        if (mapping.get("local"), mapping.get("remote")) in _pairs:
                            mapping["bisync_ready"] = True

                self.store.update_job(job["id"], mark)
            if status == "ok" and not dry_run and int(job.get("version_keep_days") or 0) > 0 and job.get("kdrive_enabled", True):
                self._prune(job, log_path, env, backend if isinstance(backend, dict) else self.store.backend())
            log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
            if status == "cancelled":
                summary = summary or "abgebrochen"
            elif status == "error":
                summary = engine.summarize_log(log_text) or summary or "rclone meldet einen Fehler"
            elif dry_run:
                actions, _total = engine.parse_dry_run(log_text, limit=-1)
                summary = engine.plan_summary(actions)
            else:
                summary = engine.summarize_log(log_text)
        except Exception as exc:
            status = "error"
            summary = str(exc)[:500]
            log.exception("Lauf %s", run_id)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"\n# Fehler: {exc}\n")
        finally:
            self._cancel.discard(job["id"])
            with self._lock:
                self._procs.pop(job["id"], None)
            self.store.finish_run(run_id, status=status, summary=summary)
            self.store.update_job(
                job["id"],
                lambda item, _status=status, _summary=summary: item.update(
                    last_status=_status,
                    last_summary=_summary,
                    last_run_at=datetime.now(self.tz).isoformat(),
                ),
            )

    def _append_combined(self, log_path: Path, combined_path: Path | None) -> None:
        if combined_path is None or not combined_path.is_file():
            return
        report = combined_path.read_text(encoding="utf-8", errors="replace").strip()
        combined_path.unlink(missing_ok=True)
        if not report:
            return
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("# COMBINED\n")
            handle.write(report)
            handle.write("\n")

    def _run_process(self, job_id: str, argv: list[str], log_path: Path, env: dict) -> int:
        kwargs = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **kwargs,
        )
        with self._lock:
            self._procs[job_id] = proc
        assert proc.stdout is not None
        with log_path.open("a", encoding="utf-8") as handle:
            for line in proc.stdout:
                handle.write(line)
                handle.flush()
        return proc.wait()

    def _terminate(self, proc: subprocess.Popen) -> None:
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                return
            except OSError:
                pass
        proc.terminate()

    def _prune(self, job: dict, log_path: Path, env: dict, backend: dict) -> None:
        keep = int(job.get("version_keep_days") or 0)
        slug = engine.slugify(job["name"], job["id"])
        now = datetime.now(self.tz)
        local_root = self.store.config_dir / "versions" / slug
        if local_root.is_dir():
            old = engine.old_stamps([path.name for path in local_root.iterdir()], now=now, keep_days=keep, tz=self.tz)
            for name in old:
                target = local_root / name
                if target.is_dir() and engine.STAMP_NAME.fullmatch(name):
                    import shutil
                    shutil.rmtree(target, ignore_errors=True)
        remote_root = f"{job['version_dir'].strip('/')}/{slug}"
        remote_name = backend["remote"]
        config_path = str(backend["config_path"])
        try:
            proc = subprocess.run(
                [
                    "rclone",
                    "lsf",
                    engine.remote_spec(remote_name, remote_root),
                    "--config",
                    config_path,
                    "--dirs-only",
                    "--max-depth",
                    "1",
                ],
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"# Versionen nicht aufgeräumt: {exc}\n")
            return
        names = [line.strip() for line in (proc.stdout or "").splitlines()]
        for name in engine.old_stamps(names, now=now, keep_days=keep, tz=self.tz):
            subprocess.run(
                ["rclone", "purge", engine.remote_spec(remote_name, f"{remote_root}/{name}"), "--config", config_path],
                capture_output=True,
                text=True,
                timeout=300,
                env=env,
                check=False,
            )


def probe(store: Store, args: list[str], timeout: int = 40) -> tuple[int, str]:
    store.require_backend()
    env = store.rclone_env()
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Zeitüberschreitung. kDrive antwortet nicht.") from exc
    except FileNotFoundError as exc:
        raise ValueError("rclone ist im Container nicht vorhanden") from exc
    text = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode, text[-4000:]


def directory_names(text: str) -> list[str]:
    names = []
    for line in text.splitlines():
        raw = line.strip()
        if not raw or " NOTICE:" in raw or " ERROR:" in raw or " INFO:" in raw or " DEBUG:" in raw:
            continue
        name = raw.strip("/")
        if not name or "/" in name:
            continue
        names.append(name)
    return sorted(names, key=str.lower)
