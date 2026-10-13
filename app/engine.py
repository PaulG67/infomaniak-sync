"""Auftragsregeln und rclone-Befehle für kDrive über WebDAV.

Infomaniak dokumentiert rclone mit dem WebDAV-Backend:
https://IDkDrive.connect.kdrive.infomaniak.com
vendor = other, Benutzer = E-Mail, Passwort = Anwendungspasswort.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from croniter import croniter

MODES = ("upload", "mirror_up", "download", "mirror_down", "bisync")
EXISTING = ("overwrite", "newer", "skip", "version")
CONFLICTS = ("newer", "larger", "local", "remote", "both")
COMPARES = ("modtime", "checksum", "size")
DELETE_MODES = ("after", "during", "before")
SCHEDULE_TYPES = ("manual", "interval", "daily", "weekly", "cron")
RCLONE_EXAMPLE_PATH = "/boot/config/plugins/rclone/.rclone.conf"
RCLONE_EXAMPLE_REMOTE = "Infomaniak"
RCLONE_REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,80}$")
_RCLONE_PUBLIC_KEYS = frozenset({"type", "url", "vendor", "user"})
_RCLONE_SECRET_KEYS = frozenset({"pass", "password", "token", "bearer_token", "client_secret", "auth_token"})
WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")

MODE_TEXT = {
    "upload": "Neue und geänderte Dateien werden nach kDrive kopiert. Auf kDrive wird nichts gelöscht.",
    "mirror_up": "kDrive wird an Unraid angeglichen. Dateien, die lokal fehlen, werden auf kDrive gelöscht.",
    "download": "Neue und geänderte Dateien werden von kDrive nach Unraid kopiert. Lokal wird nichts gelöscht.",
    "mirror_down": "Unraid wird an kDrive angeglichen. Dateien, die auf kDrive fehlen, werden lokal gelöscht.",
    "bisync": "Beide Seiten werden abgeglichen. Änderungen laufen in beide Richtungen.",
}
EXISTING_TEXT = {
    "overwrite": "Abweichende Dateien werden überschrieben.",
    "newer": "Es wird nur übertragen, wenn die Quelle neuer ist.",
    "skip": "Vorhandene Dateien bleiben unangetastet.",
    "version": "Vor dem Überschreiben wandert die alte Datei in den Versionsordner.",
}
CONFLICT_TEXT = {
    "newer": "Bei einem Konflikt gewinnt die neuere Datei. Die ältere bleibt unter einem eigenen Namen erhalten.",
    "larger": "Bei einem Konflikt gewinnt die grössere Datei. Die andere bleibt unter einem eigenen Namen erhalten.",
    "local": "Bei einem Konflikt gewinnt die Datei auf Unraid.",
    "remote": "Bei einem Konflikt gewinnt die Datei auf kDrive.",
    "both": "Bei einem Konflikt bleiben beide Fassungen erhalten.",
}

SYSTEM_PATTERNS = (
    "**/.DS_Store",
    "**/Thumbs.db",
    "**/@eaDir/**",
    "**/*.partial",
    "**/*.tmp",
    "**/~$*",
    "**/*~",
    "**/.Trash-*/**",
    "**/#recycle/**",
    "appdata/infomaniak-sync/**",
)
# OpenCloud legt Metadaten in Ordner und Dateien namens .oc-nodes. Die gehören nicht in den Abgleich.
OC_NODE_PATTERNS = (
    ".oc-nodes",
    ".oc-nodes/**",
    "**/.oc-nodes",
    "**/.oc-nodes/**",
)

DURATION = re.compile(r"^(\d+(\.\d+)?(ns|us|ms|s|m|h|d|w|M|y))+$")
SIZE = re.compile(r"^\d+(\.\d+)?([KkMmGgTtPpEe]i?)?[Bb]?$")
BANDWIDTH = re.compile(r"^[0-9A-Za-z:.,_\- ]{1,120}$")
DRIVE_ID = re.compile(r"^[0-9]{1,15}$")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
STAMP_NAME = re.compile(r"^\d{8}-\d{6}$")
MIRROR_MODES = {"mirror_up", "mirror_down"}


def blank_job() -> dict:
    return {
        "name": "",
        "enabled": True,
        "mappings": [{"local": "", "remote": ""}],
        "mode": "upload",
        "existing": "newer",
        "conflict": "newer",
        "version_dir": "_Versionen",
        "version_keep_days": 30,
        "compare": "modtime",
        "transfers": 4,
        "bandwidth": "",
        "min_age": "1m",
        "max_size": "",
        "exclude_system": True,
        "excludes": "",
        "create_empty_dirs": True,
        "copy_links": False,
        "track_renames": False,
        "verbose": False,
        "delete_mode": "after",
        "max_delete": 50,
        "confirm_delete": False,
        "confirm_root": False,
        "schedule": {
            "type": "manual",
            "interval_minutes": 60,
            "time": "03:00",
            "weekday": 0,
            "cron": "0 3 * * *",
        },
    }


def as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def clean_rel(value, *, label: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    text = text.strip("/")
    if not text:
        return ""
    parts = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError(f"{label}: «..» ist nicht erlaubt")
        if any(ord(ch) < 32 for ch in part):
            raise ValueError(f"{label} enthält ungültige Zeichen")
        parts.append(part)
    if not parts:
        return ""
    if len("/".join(parts)) > 1000:
        raise ValueError(f"{label} ist zu lang")
    return "/".join(parts)


def local_path(shares_root: Path, rel: str) -> Path:
    root = shares_root.resolve()
    target = root if not rel else (root / rel).resolve()
    try:
        if target != root:
            target.relative_to(root)
    except ValueError as exc:
        raise ValueError("Ordner liegt ausserhalb der Shares") from exc
    if not target.is_dir():
        shown = rel or "/"
        raise ValueError(f"Ordner nicht gefunden: {shown}")
    return target


def webdav_url(drive_id: str) -> str:
    return f"https://{drive_id}.connect.kdrive.infomaniak.com"


def kdrive_target(value: str) -> tuple[str, str]:
    """kDrive-ID oder die url aus der rclone-Datei, zum Beispiel https://260259.connect.kdrive.infomaniak.com."""
    raw = str(value or "").strip().rstrip("/")
    if DRIVE_ID.fullmatch(raw):
        return raw, webdav_url(raw)
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    match = re.fullmatch(r"(\d{1,15})\.connect\.kdrive\.infomaniak\.com", host)
    if parts.scheme == "https" and match and parts.path in ("", "/"):
        drive_id = match.group(1)
        return drive_id, webdav_url(drive_id)
    raise ValueError(
        "WebDAV-Adresse muss wie der url-Eintrag aussehen, "
        "zum Beispiel https://260259.connect.kdrive.infomaniak.com"
    )


def validate_identity(drive_id: str, user: str, password: str | None) -> tuple[str, str]:
    drive_id, _url = kdrive_target(drive_id)
    user = str(user or "").strip()
    if not EMAIL.fullmatch(user) or len(user) > 200:
        raise ValueError("Benutzer muss die E-Mail des Infomaniak-Kontos sein, wie user in der rclone-Datei")
    if password is not None:
        if any(ch in password for ch in "\r\n\x00"):
            raise ValueError("Passwort enthält ungültige Zeichen")
        if len(password) > 2000:
            raise ValueError("Passwort ist zu lang")
    return drive_id, user


def render_rclone_conf(drive_id: str, user: str, obscured_pass: str | None = None) -> str:
    drive_id, user = validate_identity(drive_id, user, obscured_pass)
    lines = [
        "[kdrive]",
        "type = webdav",
        f"url = {webdav_url(drive_id)}",
        "vendor = other",
        f"user = {user}",
    ]
    if obscured_pass:
        lines.append(f"pass = {obscured_pass}")
    return "\n".join(lines) + "\n"


def render_remote_conf(name: str, url: str, user: str, obscured_pass: str, vendor: str = "other") -> str:
    if not RCLONE_REMOTE_NAME.fullmatch(name or ""):
        raise ValueError("rclone-Remote ist ungültig")
    _drive_id, url = kdrive_target(url)
    _drive_id, user = validate_identity(_drive_id, user, obscured_pass)
    if not obscured_pass:
        raise ValueError("pass fehlt im WebDAV-Eintrag")
    vendor = (vendor or "other").strip() or "other"
    if any(ch in vendor for ch in "\r\n") or len(vendor) > 40:
        vendor = "other"
    return (
        f"[{name}]\n"
        "type = webdav\n"
        f"url = {url}\n"
        f"vendor = {vendor}\n"
        f"user = {user}\n"
        f"pass = {obscured_pass}\n"
    )


def _public_url(url: str) -> str:
    if not url or "://" not in url:
        return url
    parts = urlsplit(url)
    if not parts.hostname:
        return ""
    host = parts.hostname
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, ""))


def read_rclone_section(text: str, name: str) -> dict[str, str] | None:
    """Ein rclone-Abschnitt inklusive pass. Nur serverseitig verwenden."""
    current = ""
    data: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]") and len(line) > 2:
            if data is not None:
                break
            current = line[1:-1].strip()
            data = {} if current == name else None
            continue
        if data is None or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip().lower()] = value.strip().strip('"').strip("'")
    return data


def parse_rclone_remotes(text: str) -> list[dict]:
    """WebDAV- und andere Remotes ohne Passwörter oder Tokens."""
    current: dict | None = None
    found: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]") and len(line) > 2:
            name = line[1:-1].strip()
            if RCLONE_REMOTE_NAME.fullmatch(name):
                current = {"name": name, "type": "", "url": "", "vendor": "", "user": ""}
                found.append(current)
            else:
                current = None
            continue
        if current is None or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lower()
        if key in _RCLONE_SECRET_KEYS or key not in _RCLONE_PUBLIC_KEYS:
            continue
        current[key] = value.strip().strip('"').strip("'")
    for item in found:
        item["url"] = _public_url(item["url"])
    return found


def remote_spec(remote_name: str, path: str = "") -> str:
    if not RCLONE_REMOTE_NAME.fullmatch(remote_name or ""):
        raise ValueError("rclone-Remote ist ungültig")
    path = (path or "").strip("/")
    return f"{remote_name}:{path}" if path else f"{remote_name}:"


def is_oc_nodes(path: str) -> bool:
    parts = [part for part in str(path or "").replace("\\", "/").split("/") if part and part != "."]
    return any(part == ".oc-nodes" for part in parts)


def render_filters(job: dict, extra_excludes: list[str] | None = None) -> str:
    lines: list[str] = [f"- {pat}" for pat in OC_NODE_PATTERNS]
    if job.get("exclude_system", True):
        lines.extend(f"- {pat}" for pat in SYSTEM_PATTERNS)
    version_dir = str(job.get("version_dir") or "").strip("/")
    if version_dir:
        lines.append(f"- /{version_dir}/**")
        lines.append(f"- {version_dir}/**")
    for extra in extra_excludes or []:
        extra = extra.strip().strip("/")
        if extra:
            lines.append(f"- /{extra}/**")
            lines.append(f"- {extra}/**")
    raw = str(job.get("excludes") or "").replace("\r\n", "\n").replace("\r", "\n")
    user_lines = [ln.strip() for ln in raw.split("\n")]
    user_lines = [ln for ln in user_lines if ln]
    if len(user_lines) > 200:
        raise ValueError("Höchstens 200 Ausschlusszeilen")
    for line in user_lines:
        if len(line) > 300:
            raise ValueError("Eine Ausschlusszeile ist zu lang")
        if line.startswith("#"):
            lines.append(line)
            continue
        if line[0] in "+-!":
            lines.append(line)
        else:
            lines.append(f"- {line}")
    return ("\n".join(lines) + "\n") if lines else ""


def option_signature(job: dict) -> str:
    payload = {
        "compare": job["compare"],
        "min_age": job["min_age"],
        "max_size": job["max_size"],
        "excludes": job["excludes"],
        "exclude_system": job["exclude_system"],
        "copy_links": job["copy_links"],
        "create_empty_dirs": job["create_empty_dirs"],
        "version_dir": job["version_dir"],
    }
    raw = repr(sorted(payload.items())).encode()
    return hashlib.sha256(raw).hexdigest()


def _parse_time(value: str) -> str:
    match = re.fullmatch(r"(\d{2}):(\d{2})(?::\d{2})?", str(value or "").strip())
    if not match:
        raise ValueError("Uhrzeit als HH:MM angeben")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError("Uhrzeit ungültig")
    return f"{hour:02d}:{minute:02d}"


def _schedule(raw) -> dict:
    src = raw if isinstance(raw, dict) else {}
    kind = str(src.get("type") or "manual")
    if kind not in SCHEDULE_TYPES:
        raise ValueError("Zeitplan ungültig")
    try:
        interval = int(src.get("interval_minutes") or 60)
    except (TypeError, ValueError) as exc:
        raise ValueError("Intervall ungültig") from exc
    if interval < 5 or interval > 7 * 24 * 60:
        raise ValueError("Intervall zwischen 5 Minuten und 7 Tagen wählen")
    try:
        weekday = int(src.get("weekday") if src.get("weekday") is not None else 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("Wochentag ungültig") from exc
    if weekday < 0 or weekday > 6:
        raise ValueError("Wochentag ungültig")
    cron = " ".join(str(src.get("cron") or "").split())
    if kind == "cron":
        if len(cron.split()) != 5:
            raise ValueError("Cron braucht 5 Felder: Minute Stunde Tag Monat Wochentag")
        try:
            croniter(cron)
        except (ValueError, KeyError) as exc:
            raise ValueError("Cron-Ausdruck ungültig") from exc
    return {
        "type": kind,
        "interval_minutes": interval,
        "time": _parse_time(str(src.get("time") or "03:00")),
        "weekday": weekday,
        "cron": cron or "0 3 * * *",
    }


def aware(now: datetime, tz: ZoneInfo) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def next_occurrence(schedule: dict, now: datetime, tz: ZoneInfo) -> datetime | None:
    now = aware(now, tz)
    kind = schedule["type"]
    if kind == "manual":
        return None
    if kind == "interval":
        return now + timedelta(minutes=int(schedule["interval_minutes"]))
    hour, minute = (int(part) for part in schedule["time"].split(":"))
    if kind == "daily":
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    if kind == "weekly":
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        days = (int(schedule["weekday"]) - candidate.weekday()) % 7
        candidate += timedelta(days=days)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate
    nxt = croniter(schedule["cron"], now).get_next(datetime)
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=tz)
    else:
        nxt = nxt.astimezone(tz)
    return nxt


def schedule_label(job: dict) -> str:
    schedule = job["schedule"]
    kind = schedule["type"]
    if kind == "manual":
        return "nur manuell"
    if kind == "interval":
        minutes = int(schedule["interval_minutes"])
        if minutes % 1440 == 0:
            days = minutes // 1440
            return f"alle {days} Tag" if days == 1 else f"alle {days} Tage"
        if minutes % 60 == 0:
            hours = minutes // 60
            return f"alle {hours} Stunde" if hours == 1 else f"alle {hours} Stunden"
        return f"alle {minutes} Minuten"
    if kind == "daily":
        return f"täglich um {schedule['time']}"
    if kind == "weekly":
        return f"{WEEKDAYS[int(schedule['weekday'])]} um {schedule['time']}"
    return f"Cron {schedule['cron']}"


def slugify(name: str, job_id: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-._")[:40]
    if not base:
        base = "auftrag"
    return f"{base}-{job_id[:8]}"


def backup_prefix(job: dict) -> str:
    version_dir = str(job.get("version_dir") or "").strip("/")
    return f"{version_dir}/{slugify(job['name'], job['id'])}".strip("/")


def _overlaps(sync_remote: str, backup: str) -> bool:
    sync_remote = sync_remote.strip("/")
    backup = backup.strip("/")
    if not sync_remote or not backup:
        return False
    return sync_remote == backup or sync_remote.startswith(backup + "/") or backup.startswith(sync_remote + "/")


def bisync_signature_matches(old: dict | None, job: dict) -> bool:
    if not old or old.get("mode") != "bisync" or job["mode"] != "bisync":
        return False
    return old.get("option_signature") == job["option_signature"]


def prepare_job(
    raw: dict,
    *,
    old: dict | None,
    shares_root: Path,
    now: datetime,
    tz: ZoneInfo,
    relax_confirm: bool = False,
) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Ungültige Daten")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError("Name fehlt")
    if len(name) > 80 or any(ord(ch) < 32 for ch in name):
        raise ValueError("Name ist ungültig")
    mode = str(raw.get("mode") or "upload")
    if mode not in MODES:
        raise ValueError("Synchronisationsart ungültig")
    existing = str(raw.get("existing") or "newer")
    if existing not in EXISTING:
        raise ValueError("Umgang mit vorhandenen Dateien ungültig")
    conflict = str(raw.get("conflict") or "newer")
    if conflict not in CONFLICTS:
        raise ValueError("Konfliktregel ungültig")
    compare = str(raw.get("compare") or "modtime")
    if compare not in COMPARES:
        raise ValueError("Vergleich ungültig")
    delete_mode = str(raw.get("delete_mode") or "after")
    if delete_mode not in DELETE_MODES:
        raise ValueError("Löschzeitpunkt ungültig")
    try:
        transfers = int(raw.get("transfers") or 4)
        keep_days = int(raw.get("version_keep_days") or 0)
        max_delete = int(raw.get("max_delete") or 50)
    except (TypeError, ValueError) as exc:
        raise ValueError("Zahl ungültig") from exc
    if transfers < 1 or transfers > 16:
        raise ValueError("Parallele Übertragungen zwischen 1 und 16 wählen")
    if keep_days < 0 or keep_days > 3650:
        raise ValueError("Aufbewahrung der Versionen zwischen 0 und 3650 Tagen")
    if max_delete < 1 or max_delete > 100:
        raise ValueError("Löschgrenze zwischen 1 und 100 Prozent")
    min_age = str(raw.get("min_age") or "").strip()
    max_size = str(raw.get("max_size") or "").strip()
    bandwidth = str(raw.get("bandwidth") or "").strip()
    if min_age and not DURATION.fullmatch(min_age):
        raise ValueError("Mindestalter ungültig, zum Beispiel 1m oder 2h")
    if max_size and not SIZE.fullmatch(max_size):
        raise ValueError("Maximale Dateigrösse ungültig, zum Beispiel 2G")
    if bandwidth and not BANDWIDTH.fullmatch(bandwidth):
        raise ValueError("Bandbreitenlimit ungültig, zum Beispiel 10M")
    excludes = str(raw.get("excludes") or "")
    if len(excludes) > 8000:
        raise ValueError("Ausschlüsse sind zu lang")
    version_dir = clean_rel(raw.get("version_dir") or "_Versionen", label="Versionsordner")
    if not version_dir:
        raise ValueError("Versionsordner fehlt")
    mappings_raw = raw.get("mappings")
    if not isinstance(mappings_raw, list) or not mappings_raw:
        raise ValueError("Mindestens ein Ordnerpaar")
    if len(mappings_raw) > 100:
        raise ValueError("Höchstens 100 Ordnerpaare")
    confirm_root = as_bool(raw.get("confirm_root"))
    mappings = []
    seen: set[str] = set()
    for item in mappings_raw:
        if not isinstance(item, dict):
            raise ValueError("Ordnerpaar ungültig")
        local = clean_rel(item.get("local"), label="Unraid-Ordner")
        remote = clean_rel(item.get("remote"), label="kDrive-Ordner")
        if local in seen:
            raise ValueError(f"Unraid-Ordner doppelt: {local or '/'}")
        seen.add(local)
        if local == "" and not confirm_root and not relax_confirm:
            raise ValueError("Bestätige, dass der gesamte gemountete Inhalt synchronisiert werden soll.")
        local_path(shares_root, local)
        mappings.append({"local": local, "remote": remote, "bisync_ready": False})
    if mode in MIRROR_MODES and not as_bool(raw.get("confirm_delete")) and not relax_confirm:
        if mode == "mirror_down":
            raise ValueError("Bestätige das Löschen lokaler Dateien, die auf kDrive fehlen.")
        raise ValueError("Bestätige das Löschen von kDrive-Dateien, die lokal fehlen.")
    job_id = str((old or {}).get("id") or "")
    if not re.fullmatch(r"[0-9a-f-]{36}", job_id):
        job_id = str(uuid.uuid4())
    job = {
        "id": job_id,
        "name": name,
        "enabled": as_bool(raw.get("enabled"), True),
        "mappings": mappings,
        "mode": mode,
        "existing": existing,
        "conflict": conflict,
        "version_dir": version_dir,
        "version_keep_days": keep_days,
        "compare": compare,
        "transfers": transfers,
        "bandwidth": bandwidth,
        "min_age": min_age,
        "max_size": max_size,
        "exclude_system": as_bool(raw.get("exclude_system"), True),
        "excludes": excludes.replace("\r\n", "\n").replace("\r", "\n"),
        "create_empty_dirs": as_bool(raw.get("create_empty_dirs"), True),
        "copy_links": as_bool(raw.get("copy_links")),
        "track_renames": as_bool(raw.get("track_renames")),
        "verbose": as_bool(raw.get("verbose")),
        "delete_mode": delete_mode,
        "max_delete": max_delete,
        "confirm_delete": as_bool(raw.get("confirm_delete")),
        "confirm_root": confirm_root,
        "schedule": _schedule(raw.get("schedule")),
        "created_at": (old or {}).get("created_at") or aware(now, tz).isoformat(),
        "updated_at": aware(now, tz).isoformat(),
        "last_run_at": (old or {}).get("last_run_at"),
        "last_status": (old or {}).get("last_status"),
        "last_summary": (old or {}).get("last_summary") or "",
    }
    render_filters(job)
    job["option_signature"] = option_signature(job)
    prefix = backup_prefix(job)
    for mapping in job["mappings"]:
        if job["existing"] == "version" and _overlaps(mapping["remote"], prefix):
            raise ValueError(
                "Der Versionsordner liegt im synchronisierten kDrive-Ordner. "
                "Wähle einen Ordner daneben, zum Beispiel _Versionen."
            )
    ready_by_pair: dict[tuple[str, str], bool] = {}
    if bisync_signature_matches(old, job):
        for mapping in old.get("mappings") or []:
            ready_by_pair[(mapping.get("local", ""), mapping.get("remote", ""))] = bool(mapping.get("bisync_ready"))
    for mapping in job["mappings"]:
        mapping["bisync_ready"] = bool(ready_by_pair.get((mapping["local"], mapping["remote"]), False))
    if old and old.get("schedule") == job["schedule"] and old.get("next_run"):
        job["next_run"] = old.get("next_run")
    else:
        nxt = next_occurrence(job["schedule"], now, tz)
        job["next_run"] = nxt.isoformat() if nxt else None
    return job


def schedule_block(job: dict) -> str:
    if job.get("mode") != "bisync":
        return ""
    if all(mapping.get("bisync_ready") for mapping in job.get("mappings") or []):
        return ""
    return "Zeitplan wartet auf den einmaligen Erstabgleich."


def describe(job: dict) -> str:
    pairs = []
    for mapping in job["mappings"]:
        src = "der gesamte gemountete Inhalt" if not mapping["local"] else mapping["local"]
        dst = "die oberste kDrive-Ebene" if not mapping["remote"] else mapping["remote"]
        pairs.append(f"{src} → {dst}")
    parts = [MODE_TEXT[job["mode"]], ]
    if job["mode"] == "bisync":
        parts.append(CONFLICT_TEXT[job["conflict"]])
    else:
        parts.append(EXISTING_TEXT[job["existing"]])
    if job["existing"] == "version":
        parts.append(f"Versionen liegen unter {job['version_dir']}.")
    parts.append(f"Zeitplan: {schedule_label(job)}.")
    parts.append("Ordner: " + "; ".join(pairs) + ".")
    if job.get("exclude_system", True):
        parts.append("System- und Temporärdateien werden ausgelassen.")
    if job["min_age"]:
        parts.append(f"Dateien jünger als {job['min_age']} warten bis zum nächsten Lauf.")
    block = schedule_block(job)
    if block:
        parts.append(block)
    return " ".join(parts)


def public_job(job: dict) -> dict:
    data = dict(job)
    data["description"] = describe(job)
    data["schedule_label"] = schedule_label(job)
    data["schedule_block"] = schedule_block(job)
    data["bisync_ready"] = bool(job.get("mappings")) and all(
        mapping.get("bisync_ready") for mapping in job["mappings"]
    )
    return data


def config_exclude(config_dir: Path, shares_root: Path) -> str | None:
    try:
        rel = config_dir.resolve().relative_to(shares_root.resolve()).as_posix()
    except ValueError:
        return None
    if not rel or rel == ".":
        return None
    return rel


def build_commands(
    job: dict,
    *,
    shares_root: Path,
    config_path: Path,
    filter_path: Path,
    workdir_base: Path,
    version_root: Path,
    dry_run: bool = False,
    resync: bool = False,
    stamp: str,
    remote_name: str = "kdrive",
) -> list[list[str]]:
    commands = []
    slug = slugify(job["name"], job["id"])
    for index, mapping in enumerate(job["mappings"]):
        local = local_path(shares_root, mapping["local"])
        remote = mapping["remote"]
        src_is_local = job["mode"] in {"upload", "mirror_up", "bisync"}
        local_s = str(local)
        remote_s = remote_spec(remote_name, remote)
        if job["mode"] == "bisync":
            argv = ["rclone", "bisync", local_s, remote_s]
        elif src_is_local:
            op = "copy" if job["mode"] == "upload" else "sync"
            argv = ["rclone", op, local_s, remote_s]
        else:
            op = "copy" if job["mode"] == "download" else "sync"
            argv = ["rclone", op, remote_s, local_s]
        argv.extend(
            [
                "--config",
                str(config_path),
                "--transfers",
                str(job["transfers"]),
                "--checkers",
                str(max(8, int(job["transfers"]))),
                "--retries",
                "5",
                "--retries-sleep",
                "10s",
                "--low-level-retries",
                "10",
                "--timeout",
                "10m",
                "--contimeout",
                "60s",
                "--stats",
                "15s",
                "--stats-log-level",
                "NOTICE",
                "--log-level",
                "DEBUG" if dry_run else ("INFO" if job.get("verbose") else "NOTICE"),
            ]
        )
        if job["mode"] == "bisync":
            digest = hashlib.sha1(f"{mapping['local']}\n{mapping['remote']}".encode()).hexdigest()[:12]
            argv.extend(
                [
                    "--workdir",
                    str(workdir_base / job["id"] / digest),
                    "--filters-file",
                    str(filter_path),
                    "--resilient",
                    "--recover",
                    "--max-delete",
                    str(job["max_delete"]),
                    "--conflict-loser",
                    "pathname",
                    "--conflict-suffix",
                    "unraid,kdrive",
                ]
            )
            if job["conflict"] == "both":
                argv.extend(["--conflict-resolve", "none"])
            else:
                resolved = {"newer": "newer", "larger": "larger", "local": "path1", "remote": "path2"}[
                    job["conflict"]
                ]
                argv.extend(["--conflict-resolve", resolved])
            compare = {"modtime": "size,modtime", "checksum": "size,modtime,checksum", "size": "size"}[job["compare"]]
            argv.extend(["--compare", compare])
            if resync:
                resync_mode = {
                    "newer": "newer",
                    "larger": "larger",
                    "local": "path1",
                    "remote": "path2",
                    "both": "newer",
                }[job["conflict"]]
                argv.extend(["--resync-mode", resync_mode])
        else:
            argv.extend(["--filter-from", str(filter_path)])
            if job["existing"] == "newer":
                argv.append("--update")
            elif job["existing"] == "skip":
                argv.append("--ignore-existing")
            if job["compare"] == "checksum" and job["existing"] != "newer":
                argv.append("--checksum")
            elif job["compare"] == "size" and job["existing"] != "newer":
                argv.append("--size-only")
            if job["mode"] in MIRROR_MODES:
                if job.get("track_renames"):
                    argv.extend(["--track-renames", "--delete-after"])
                else:
                    argv.append(
                        {"after": "--delete-after", "during": "--delete-during", "before": "--delete-before"}[
                            job["delete_mode"]
                        ]
                    )
        if job.get("create_empty_dirs"):
            argv.append("--create-empty-src-dirs")
        argv.append("--copy-links" if job.get("copy_links") else "--skip-links")
        if job.get("min_age"):
            argv.extend(["--min-age", job["min_age"]])
        if job.get("max_size"):
            argv.extend(["--max-size", job["max_size"]])
        if job.get("bandwidth"):
            argv.extend(["--bwlimit", job["bandwidth"]])
        use_backup = job["existing"] == "version" or (job["mode"] == "bisync" and resync)
        if use_backup:
            leaf = f"{stamp}-{index}"
            local_backup = version_root / slug / stamp / str(index)
            remote_backup = f"{job['version_dir'].strip('/')}/{slug}/{stamp}/{index}"
            if job["mode"] == "bisync":
                argv.extend(
                    [
                        "--backup-dir1",
                        str(local_backup),
                        "--backup-dir2",
                        remote_spec(remote_name, remote_backup),
                        "--suffix",
                        leaf,
                        "--suffix-keep-extension",
                    ]
                )
            elif job["mode"] in {"upload", "mirror_up"}:
                argv.extend(
                    ["--backup-dir", remote_spec(remote_name, remote_backup), "--suffix", leaf, "--suffix-keep-extension"]
                )
            else:
                argv.extend(["--backup-dir", str(local_backup), "--suffix", leaf, "--suffix-keep-extension"])
        if dry_run:
            argv.extend(["--dry-run", "--use-json-log"])
        commands.append(argv)
    return commands


def format_command(argv: list[str]) -> str:
    return shlex.join(argv)


def _join_root(root: str, rel: str) -> str:
    rel = (rel or "").replace("\\", "/").strip("/")
    root = (root or "").replace("\\", "/").strip("/")
    if not rel:
        return root or "/"
    return f"{root}/{rel}" if root else rel


def _dry_label(kind: str, direction: str, version: bool) -> str:
    arrow = {"up": "Unraid → kDrive", "down": "kDrive → Unraid"}.get(direction, "beide Seiten")
    if kind == "copy":
        return f"Kopieren · {arrow}"
    if kind == "overwrite":
        extra = ", alte Fassung in den Versionsordner" if version else ""
        return f"Überschreiben{extra} · {arrow}"
    if kind == "move":
        return f"Verschieben · {arrow}"
    if kind == "time":
        return f"Zeitstempel · {arrow}"
    if kind == "delete" and direction == "remote":
        return "Löschen auf kDrive"
    if kind == "delete" and direction == "local":
        return "Löschen auf Unraid"
    if kind == "delete":
        return "Löschen"
    if kind == "error":
        return f"Fehler · {arrow}"
    return arrow


def _dry_paths(kind: str, direction: str, local_root: str, remote_root: str, obj: str) -> tuple[str, str]:
    if kind in {"delete", "keep"} and direction == "remote":
        return "", _join_root(remote_root, obj)
    if kind in {"delete", "keep"} and direction == "local":
        return _join_root(local_root, obj), ""
    return _join_root(local_root, obj), _join_root(remote_root, obj)


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", _ANSI.sub("", text or "")).strip()


def _side_states(kind: str, way: str) -> tuple[str, str]:
    """Zustand der Datei auf Unraid und auf kDrive: vorhanden, erstellt, überschrieben, gelöscht."""
    if kind == "copy" and way == "down":
        return "create", "exists"
    if kind == "copy" and way == "up":
        return "exists", "create"
    if kind in {"overwrite", "time"} and way == "down":
        return "overwrite" if kind == "overwrite" else "time", "exists"
    if kind in {"overwrite", "time"} and way == "up":
        return "exists", "overwrite" if kind == "overwrite" else "time"
    if kind == "delete" and way == "local":
        return "delete", "absent"
    if kind == "delete" and way == "remote":
        return "absent", "delete"
    if kind == "keep" and way == "local":
        return "exists", "absent"
    if kind == "keep" and way == "remote":
        return "absent", "exists"
    if kind == "move" and way == "down":
        return "create", "exists"
    if kind == "move" and way == "up":
        return "exists", "create"
    return "absent", "absent"


def _note_phase(msg: str, phase: str | None, delete_side: str | None) -> tuple[str | None, str | None]:
    low = _plain(msg).lower().replace(" - ", " ")
    # Path1 ist Unraid, Path2 ist kDrive. "copy to Path1" schreibt nach Unraid.
    if "copy to path1" in low or "copies to path1" in low:
        phase = "down"
    elif "copy to path2" in low or "copies to path2" in low:
        phase = "up"
    elif "path1" in low and "path2" in low:
        if "to path2" in low or "path1 to path2" in low:
            phase = "up"
        elif "to path1" in low or "path2 to path1" in low:
            phase = "down"
    if "delet" in low and "path2" in low and "path1" not in low:
        delete_side = "remote"
    elif "delet" in low and "path1" in low and "path2" not in low:
        delete_side = "local"
    return phase, delete_side


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_BISYNC_CHANGE = re.compile(
    r"Path(?P<side>[12])\s+(?P<what>File is new|File is newer|File was deleted)\s+-\s+(?P<path>.+?)\s*$",
    re.IGNORECASE,
)


def _object_name(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("remote", "path", "name"):
            found = value.get(key)
            if isinstance(found, str) and found:
                return found
    return ""


def parse_dry_run(text: str, limit: int = 400) -> tuple[list[dict], int]:
    """Dateiaktionen eines rclone-Probelaufs: Unraid, Vorgang, kDrive."""
    local_root = ""
    remote_root = ""
    direction = "up"
    version = False
    phase: str | None = None
    delete_side: str | None = None
    missing: set[str] = set()
    rows: list[dict] = []
    seen: set[tuple] = set()
    section_start = 0
    in_combined = False
    deletes = True
    bisync_seen = False
    plain = re.compile(
        r"^(?:\d{4}/\d{2}/\d{2}[ T]\d{2}:\d{2}:\d{2}\s+)?(?:NOTICE|INFO|DEBUG)\s*:?\s*(?P<body>.*)$",
        re.IGNORECASE,
    )

    def add(kind: str, obj: str, way: str, label: str | None = None) -> None:
        if not obj or obj in {".", "/"}:
            return
        obj = obj.replace("\\", "/").strip().strip("/")
        if not obj or is_oc_nodes(obj):
            return
        local, remote = _dry_paths(kind, way, local_root, remote_root, obj)
        key = (local, remote, kind, way)
        if key in seen:
            return
        seen.add(key)
        local_state, remote_state = _side_states(kind, way)
        rows.append(
            {
                "local": local,
                "remote": remote,
                "local_state": local_state,
                "remote_state": remote_state,
                "kind": kind,
                "direction": way,
                "label": label or _dry_label(kind, way, version),
            }
        )

    def add_bisync(side: str, what: str, path: str) -> None:
        nonlocal bisync_seen
        bisync_seen = True
        if what == "new" and side == "2":
            add("copy", path, "down", "Neu auf kDrive · kopieren nach Unraid")
        elif what == "new" and side == "1":
            add("copy", path, "up", "Neu auf Unraid · kopieren nach kDrive")
        elif what == "newer" and side == "2":
            add("overwrite", path, "down", "kDrive ist neuer · kDrive → Unraid")
        elif what == "newer" and side == "1":
            add("overwrite", path, "up", "Unraid ist neuer · Unraid → kDrive")
        elif what == "deleted" and side == "2":
            add("delete", path, "local", "Auf kDrive gelöscht · löschen auf Unraid")
        elif what == "deleted" and side == "1":
            add("delete", path, "remote", "Auf Unraid gelöscht · löschen auf kDrive")

    def take_bisync(text: str) -> bool:
        matched_change = _BISYNC_CHANGE.search(_plain(text))
        if not matched_change:
            return False
        what = matched_change.group("what").lower()
        if what.startswith("file is new"):
            kind = "new"
        elif what.startswith("file is newer"):
            kind = "newer"
        else:
            kind = "deleted"
        add_bisync(matched_change.group("side"), kind, matched_change.group("path"))
        return True

    def consider(level: str, msg: str, obj: str) -> None:
        nonlocal phase, delete_side
        msg = _plain(msg)
        obj = _plain(obj)
        low = msg.lower()
        if "directory modification time" in low or "directory modtime" in low:
            return
        if obj and (
            "doesn't exist" in low
            or "does not exist" in low
            or "not found at destination" in low
            or ("need to transfer" in low and "not found" in low)
        ):
            missing.add(obj)
            return
        if obj and "unchanged skipping" in low:
            missing.discard(obj)
            return
        if bisync_seen and ("skipped copy" in low or "skipped delete" in low or "skipped move" in low):
            return
        if "skipped copy" in low or "not copying as --dry-run" in low:
            if not obj and ": " in msg:
                obj, msg = msg.split(": ", 1)
                low = msg.lower()
            kind = "copy" if obj in missing else "overwrite"
            missing.discard(obj)
            way = phase or direction
            if way == "both":
                way = "up"
            label = None
            if kind == "copy" and way == "down":
                label = "Neu auf kDrive · kopieren nach Unraid"
            elif kind == "copy" and way == "up":
                label = "Neu auf Unraid · kopieren nach kDrive"
            add(kind, obj, way if way in {"up", "down"} else "up", label)
            return
        if "skipped delete" in low or "not deleting as --dry-run" in low:
            if not obj and ": " in msg:
                obj = msg.split(": ", 1)[0]
            if direction == "down":
                side = "local"
            elif direction == "up":
                side = "remote"
            else:
                side = delete_side or "remote"
            add("delete", obj, side)
            return
        if "skipped move" in low:
            way = phase or direction
            add("move", obj, "down" if way == "down" else "up")
            return
        if "modification time" in low and ("skipped" in low or "not updating" in low):
            way = phase or direction
            add("time", obj, "down" if way == "down" else "up")
            return
        phase, delete_side = _note_phase(msg, phase, delete_side)

    def drop_section() -> None:
        dropped = rows[section_start:]
        del rows[section_start:]
        for row in dropped:
            seen.discard((row["local"], row["remote"], row["kind"], row["direction"]))

    def apply_combined(symbol: str, obj: str) -> None:
        way = phase or direction
        if way not in {"up", "down"}:
            way = "up"
        if symbol == "+":
            add("copy", obj, way)
        elif symbol == "*":
            add("overwrite", obj, way)
        elif symbol == "-":
            if direction == "down":
                side = "local"
            elif direction == "up":
                side = "remote"
            else:
                side = delete_side or "remote"
            if deletes:
                add("delete", obj, side)
            elif side == "local":
                add("keep", obj, side, "Nur auf Unraid, wird nicht verändert")
            else:
                add("keep", obj, side, "Nur auf kDrive, wird nicht verändert")
        elif symbol == "!":
            add("error", obj, way)

    for raw in text.splitlines():
        line = _ANSI.sub("", raw).strip()
        if not line:
            continue
        if line.startswith("# PLAN "):
            in_combined = False
            bisync_seen = False
            section_start = len(rows)
            try:
                marker = json.loads(line[7:])
            except json.JSONDecodeError:
                continue
            local_root = str(marker.get("local") or "")
            remote_root = str(marker.get("remote") or "")
            direction = str(marker.get("dir") or "up")
            version = bool(marker.get("version"))
            deletes = bool(marker.get("deletes", True))
            phase = None
            delete_side = None
            missing.clear()
            continue
        if line == "# COMBINED":
            drop_section()
            in_combined = True
            continue
        if in_combined:
            matched_change = re.match(r"^([+*\-!])\s+(.+)$", line)
            if matched_change:
                apply_combined(matched_change.group(1), matched_change.group(2).strip())
            continue
        if line.startswith("{"):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                msg = str(item.get("msg") or "")
                if take_bisync(msg):
                    continue
                skipped = str(item.get("skipped") or "")
                if skipped and f"skipped {skipped}" not in msg.lower():
                    msg = f"Skipped {skipped} as --dry-run is set"
                consider(str(item.get("level") or ""), msg, _object_name(item.get("object")))
            continue
        matched = plain.match(line)
        if not matched:
            take_bisync(line)
            continue
        body = matched.group("body")
        if take_bisync(body):
            continue
        obj = ""
        msg = body
        if ": " in body:
            obj, msg = body.split(": ", 1)
        consider("", msg, obj)
    total = len(rows)
    if limit >= 0:
        rows = rows[:limit]
    return rows, total


def plan_summary(actions: list[dict]) -> str:
    if not actions:
        return "Probelauf: keine Änderungen"
    names = {
        "copy": "kopieren",
        "overwrite": "überschreiben",
        "delete": "löschen",
        "move": "verschieben",
        "time": "Zeitstempel",
        "error": "Fehler",
        "keep": "nur auf einer Seite",
    }
    counts: dict[str, int] = {}
    for item in actions:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    parts = [f"{counts[kind]} {names[kind]}" for kind in names if kind in counts]
    return "Probelauf: " + ", ".join(parts)


def summarize_log(text: str) -> str:
    keys = ("Transferred:", "Errors:", "Checks:", "Elapsed time:")
    found: dict[str, str] = {}
    last = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        last = line
        for key in keys:
            if key in line:
                found[key] = line.split(key, 1)[1].strip()
                found[key] = f"{key} {found[key]}"
    if not found:
        return last[:400]
    return " | ".join(found[key] for key in keys if key in found)[:500]


def old_stamps(names: list[str], *, now: datetime, keep_days: int, tz: ZoneInfo) -> list[str]:
    if keep_days <= 0:
        return []
    now = aware(now, tz)
    cutoff = now - timedelta(days=keep_days)
    chosen = []
    for name in names:
        name = name.strip().strip("/")
        if not name or "/" in name or not STAMP_NAME.fullmatch(name):
            continue
        stamp = datetime.strptime(name, "%Y%m%d-%H%M%S").replace(tzinfo=tz)
        if stamp < cutoff:
            chosen.append(name)
    return chosen


def needs_resync(job: dict) -> bool:
    return job.get("mode") == "bisync" and not all(m.get("bisync_ready") for m in job.get("mappings") or [])
