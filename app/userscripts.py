"""Vorhandene Unraid-User-Scripts lesen und ihren Zeitplan schreiben.

Das Plugin speichert jedes Skript unter
/boot/config/plugins/user.scripts/scripts/<ordner>/script
und den Zeitplan in schedule.json. Eine eigene Cron-Zeile landet in
customSchedule.cron. Stündlich bis monatlich liest das Plugin zusätzlich
/tmp/user.scripts/schedule.json.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HOST_ROOT = "/boot/config/plugins/user.scripts"
FREQUENCIES = ("disabled", "hourly", "daily", "weekly", "monthly", "start", "stop", "boot", "custom")
FREQUENCY_LABELS = {
    "disabled": "aus",
    "hourly": "stündlich",
    "daily": "täglich",
    "weekly": "wöchentlich",
    "monthly": "monatlich",
    "start": "beim Start des Arrays",
    "stop": "beim Stoppen des Arrays",
    "boot": "nur beim ersten Array-Start",
    "custom": "eigene Cron-Zeit",
}
FOLDER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.:-]{0,120}$")
CRON_FIELD = re.compile(r"^[0-9A-Za-z*/,\-]+$")
META_LINE = re.compile(r"^#(name|description)=(.*)$")
MAX_SCRIPT = 400_000


def schedule_dom_id(folder: str) -> str:
    return "schedule" + folder.replace(".", "-").replace(" ", "")


def host_script_path(folder: str) -> str:
    return f"{HOST_ROOT}/scripts/{folder}/script"


def validate_cron(expression: str) -> str:
    text = " ".join(str(expression or "").split())
    fields = text.split(" ") if text else []
    if len(fields) != 5 or any(not CRON_FIELD.fullmatch(field) or len(field) > 40 for field in fields):
        raise ValueError("Cron braucht fünf Felder, zum Beispiel 30 3 * * *")
    return text


def _meta(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines()[:40]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#!"):
            continue
        if not stripped.startswith("#"):
            break
        matched = META_LINE.match(stripped)
        if matched:
            found[matched.group(1).lower()] = matched.group(2).strip()
    return found


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)


class UserScripts:
    def __init__(self, root: Path, live: Path | None = None):
        self.root = root
        self.live = live

    @property
    def scripts_dir(self) -> Path:
        return self.root / "scripts"

    def available(self) -> bool:
        return self.scripts_dir.is_dir()

    def _folder(self, folder: str) -> str:
        name = str(folder or "").strip()
        if not FOLDER_NAME.fullmatch(name) or name in {".", ".."}:
            raise ValueError("Skriptname ist ungültig")
        return name

    def _script_file(self, folder: str) -> Path:
        path = (self.scripts_dir / folder / "script").resolve()
        root = self.scripts_dir.resolve()
        if path.parent.parent != root or path.name != "script":
            raise ValueError("Skriptname ist ungültig")
        return path

    def _load_schedule(self) -> dict:
        path = self.root / "schedule.json"
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("schedule.json von User Scripts lässt sich nicht lesen") from exc
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            found = {}
            for item in data:
                if isinstance(item, dict) and isinstance(item.get("script"), str):
                    found[item["script"]] = item
            return found
        raise ValueError("schedule.json von User Scripts hat ein unbekanntes Format")

    def _entry(self, schedule: dict, folder: str) -> dict:
        host = host_script_path(folder)
        for key, item in schedule.items():
            if not isinstance(item, dict):
                continue
            script = str(item.get("script") or key)
            if script.rstrip("/") == host or script.rstrip("/").endswith(f"/scripts/{folder}/script"):
                return item
        return {}

    def _public(self, folder: str, schedule: dict, *, include_script: bool) -> dict:
        directory = self.scripts_dir / folder
        body = _read(directory / "script").replace("\r\n", "\n").replace("\r", "\n")
        meta = _meta(body)
        name = _read(directory / "name").strip() or meta.get("name") or folder
        description = _read(directory / "description").strip() or meta.get("description") or ""
        entry = self._entry(schedule, folder)
        frequency = str(entry.get("frequency") or "disabled")
        if frequency not in FREQUENCIES:
            frequency = "disabled"
        custom = str(entry.get("custom") or "")
        item = {
            "id": folder,
            "name": name,
            "description": description,
            "frequency": frequency,
            "frequency_label": FREQUENCY_LABELS[frequency],
            "enabled": frequency != "disabled",
            "custom": custom,
        }
        if include_script:
            item["script"] = body
        return item

    def list_scripts(self) -> list[dict]:
        if not self.available():
            return []
        schedule = self._load_schedule()
        found = []
        try:
            folders = sorted(self.scripts_dir.iterdir(), key=lambda path: path.name.lower())
        except OSError:
            return []
        for folder in folders:
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            if not FOLDER_NAME.fullmatch(folder.name):
                continue
            if not (folder / "script").is_file():
                continue
            found.append(self._public(folder.name, schedule, include_script=False))
        return found

    def get_script(self, folder: str) -> dict:
        name = self._folder(folder)
        path = self._script_file(name)
        if not path.is_file():
            raise ValueError("Skript nicht gefunden")
        return self._public(name, self._load_schedule(), include_script=True)

    def save_script(self, folder: str, body: str, *, enabled: bool, frequency: str, custom: str) -> dict:
        name = self._folder(folder)
        path = self._script_file(name)
        if not path.is_file():
            raise ValueError("Skript nicht gefunden")
        if not isinstance(body, str) or "\x00" in body:
            raise ValueError("Skripttext ist ungültig")
        if len(body) > MAX_SCRIPT:
            raise ValueError("Skript ist zu gross")
        chosen = str(frequency or "").strip()
        if not enabled:
            chosen = "disabled"
        elif chosen not in FREQUENCIES or chosen == "disabled":
            raise ValueError("Intervall ist ungültig")
        cron = validate_cron(custom) if chosen == "custom" else " ".join(str(custom or "").split())
        if chosen == "custom":
            pass
        elif cron and not all(CRON_FIELD.fullmatch(field) for field in cron.split(" ")):
            cron = ""
        text = body.replace("\r\n", "\n").replace("\r", "\n")
        if text and not text.endswith("\n"):
            text += "\n"
        _write(path, text)
        self._save_schedule(name, chosen, cron)
        return self.get_script(name)

    def _save_schedule(self, folder: str, frequency: str, custom: str) -> None:
        schedule = self._load_schedule()
        host = host_script_path(folder)
        current = self._entry(schedule, folder)
        entry = {
            "script": host,
            "frequency": frequency,
            "id": str(current.get("id") or schedule_dom_id(folder)),
            "custom": custom if frequency == "custom" else str(current.get("custom") or ""),
        }
        kept = {}
        for key, item in schedule.items():
            if not isinstance(item, dict):
                continue
            script = str(item.get("script") or key).rstrip("/")
            if script == host or script.endswith(f"/scripts/{folder}/script"):
                continue
            kept[script] = item
        kept[host] = entry
        encoded = json.dumps(kept, indent=4, ensure_ascii=False) + "\n"
        _write(self.root / "schedule.json", encoded)
        if self.live is not None and self.live.is_dir():
            try:
                _write(self.live / "schedule.json", encoded)
            except OSError:
                pass
        self._write_cron(kept)

    def _write_cron(self, schedule: dict) -> None:
        lines = []
        for item in schedule.values():
            if not isinstance(item, dict) or item.get("frequency") != "custom":
                continue
            cron = str(item.get("custom") or "").strip()
            script = str(item.get("script") or "").strip()
            if not cron or not script:
                continue
            lines.append(
                f"{cron} /usr/local/emhttp/plugins/user.scripts/startCustom.php '{script}' > /dev/null 2>&1"
            )
        path = self.root / "customSchedule.cron"
        if not lines:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        text = "# Generated cron schedule for user.scripts\n" + "\n".join(lines) + "\n\n"
        _write(path, text)
