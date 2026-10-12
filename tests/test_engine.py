from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.engine import (
    blank_job,
    build_commands,
    describe,
    kdrive_target,
    next_occurrence,
    old_stamps,
    parse_dry_run,
    parse_rclone_remotes,
    plan_summary,
    read_rclone_section,
    prepare_job,
    render_filters,
    render_rclone_conf,
)

TZ = ZoneInfo("Europe/Zurich")
NOW = datetime(2026, 10, 12, 10, 0, tzinfo=TZ)


def shares(tmp_path: Path) -> Path:
    root = tmp_path / "shares"
    (root / "photos" / "2024").mkdir(parents=True)
    (root / "docs").mkdir()
    return root


def payload(**overrides):
    job = blank_job()
    job.update(
        {
            "name": "Fotos",
            "mappings": [{"local": "photos", "remote": "Fotos"}],
            "schedule": {**job["schedule"], "type": "daily", "time": "03:00"},
        }
    )
    job.update(overrides)
    return job


def test_daily_and_weekly_schedule():
    daily = next_occurrence(
        {"type": "daily", "time": "03:00", "interval_minutes": 60, "weekday": 0, "cron": "0 3 * * *"},
        NOW,
        TZ,
    )
    assert daily == datetime(2026, 10, 13, 3, 0, tzinfo=TZ)
    weekly = next_occurrence(
        {"type": "weekly", "time": "03:00", "interval_minutes": 60, "weekday": 0, "cron": "0 3 * * *"},
        NOW,
        TZ,
    )
    assert weekly == datetime(2026, 10, 19, 3, 0, tzinfo=TZ)
    soon = next_occurrence(
        {"type": "interval", "time": "03:00", "interval_minutes": 60, "weekday": 0, "cron": "0 3 * * *"},
        NOW,
        TZ,
    )
    assert soon == datetime(2026, 10, 12, 11, 0, tzinfo=TZ)


def test_rejects_parent_and_mirror_without_confirm(tmp_path):
    root = shares(tmp_path)
    with pytest.raises(ValueError, match="nicht erlaubt"):
        prepare_job(
            payload(mappings=[{"local": "photos/../docs", "remote": "x"}]),
            old=None,
            shares_root=root,
            now=NOW,
            tz=TZ,
        )
    with pytest.raises(ValueError, match="Löschen"):
        prepare_job(payload(mode="mirror_up"), old=None, shares_root=root, now=NOW, tz=TZ)


def test_upload_command_has_no_secret(tmp_path):
    root = shares(tmp_path)
    job = prepare_job(payload(existing="newer"), old=None, shares_root=root, now=NOW, tz=TZ)
    argv = build_commands(
        job,
        shares_root=root,
        config_path=tmp_path / "rclone.conf",
        filter_path=tmp_path / "filters.txt",
        workdir_base=tmp_path / "bisync",
        version_root=tmp_path / "versions",
        stamp="20261012-100000",
    )[0]
    assert argv[:4] == ["rclone", "copy", str((root / "photos").resolve()), "kdrive:Fotos"]
    assert "--update" in argv
    assert "--delete-after" not in argv
    assert "s3cret" not in " ".join(argv)
    text = render_filters(job)
    assert "- **/@eaDir/**" in text
    assert "- /_Versionen/**" in text
    assert "nach kDrive kopiert" in describe(job)


def test_mirror_and_bisync_flags(tmp_path):
    root = shares(tmp_path)
    mirror = prepare_job(
        payload(mode="mirror_up", confirm_delete=True, existing="version"),
        old=None,
        shares_root=root,
        now=NOW,
        tz=TZ,
    )
    mirror_cmd = build_commands(
        mirror,
        shares_root=root,
        config_path=tmp_path / "rclone.conf",
        filter_path=tmp_path / "filters.txt",
        workdir_base=tmp_path / "bisync",
        version_root=tmp_path / "versions",
        stamp="20261012-100000",
    )[0]
    assert mirror_cmd[1] == "sync"
    assert "--delete-after" in mirror_cmd
    assert "--backup-dir" in mirror_cmd

    with pytest.raises(ValueError, match="Versionsordner"):
        prepare_job(
            payload(existing="version", version_dir="Fotos/alt"),
            old=None,
            shares_root=root,
            now=NOW,
            tz=TZ,
        )

    bisync = prepare_job(
        payload(mode="bisync", conflict="newer"),
        old=None,
        shares_root=root,
        now=NOW,
        tz=TZ,
    )
    bisync_cmd = build_commands(
        bisync,
        shares_root=root,
        config_path=tmp_path / "rclone.conf",
        filter_path=tmp_path / "filters.txt",
        workdir_base=tmp_path / "bisync",
        version_root=tmp_path / "versions",
        resync=True,
        stamp="20261012-100000",
    )[0]
    assert bisync_cmd[1] == "bisync"
    assert "--resilient" in bisync_cmd
    assert "--resync-mode" in bisync_cmd
    assert bisync_cmd[bisync_cmd.index("--conflict-resolve") + 1] == "newer"
    assert "Erstabgleich" in describe(bisync)


def test_old_stamps_and_conf():
    old = old_stamps(
        ["20261001-030000/", "20261012-090000", "not-a-stamp", "nested/20261001-030000"],
        now=NOW,
        keep_days=7,
        tz=TZ,
    )
    assert old == ["20261001-030000"]
    conf = render_rclone_conf("123456", "ada@example.com")
    assert "url = https://123456.connect.kdrive.infomaniak.com" in conf
    assert "pass" not in conf
    with pytest.raises(ValueError):
        render_rclone_conf("abc", "ada@example.com")


def test_kdrive_url_from_rclone_block():
    drive_id, url = kdrive_target("https://260259.connect.kdrive.infomaniak.com")
    assert drive_id == "260259"
    assert url == "https://260259.connect.kdrive.infomaniak.com"
    section = read_rclone_section(
        "[Infomaniak]\ntype = webdav\npass = OBSCURED\nurl = https://260259.connect.kdrive.infomaniak.com\nuser = paul@gehring.li\n",
        "Infomaniak",
    )
    assert section["pass"] == "OBSCURED"
    assert section["user"] == "paul@gehring.li"
    assert "pass" not in parse_rclone_remotes(
        "[Infomaniak]\ntype = webdav\npass = OBSCURED\nurl = https://260259.connect.kdrive.infomaniak.com\nuser = paul@gehring.li\n"
    )[0]


def test_parse_rclone_remotes_hides_secrets():
    text = """
[Infomaniak]
type = webdav
url = https://user:s3cret@123456.connect.kdrive.infomaniak.com/extra
vendor = other
user = ada@example.com
pass = OBSCURED-SECRET

[Lokal]
type = local

[bad name]
type = webdav
pass = nope
"""
    remotes = parse_rclone_remotes(text)
    assert [item["name"] for item in remotes] == ["Infomaniak", "Lokal"]
    info = remotes[0]
    assert info["url"] == "https://123456.connect.kdrive.infomaniak.com/extra"
    assert info["user"] == "ada@example.com"
    dumped = str(remotes)
    assert "OBSCURED-SECRET" not in dumped
    assert "s3cret" not in dumped
    assert "nope" not in dumped


def test_named_remote_in_command(tmp_path):
    root = shares(tmp_path)
    job = prepare_job(payload(existing="version"), old=None, shares_root=root, now=NOW, tz=TZ)
    argv = build_commands(
        job,
        shares_root=root,
        config_path=tmp_path / "rclone.conf",
        filter_path=tmp_path / "filters.txt",
        workdir_base=tmp_path / "bisync",
        version_root=tmp_path / "versions",
        stamp="20261012-100000",
        remote_name="Infomaniak",
    )[0]
    assert "Infomaniak:Fotos" in argv
    assert any(part.startswith("Infomaniak:_Versionen/") for part in argv)


def test_dry_run_uses_debug_json_log(tmp_path):
    root = shares(tmp_path)
    job = prepare_job(payload(existing="newer"), old=None, shares_root=root, now=NOW, tz=TZ)
    dry = build_commands(
        job,
        shares_root=root,
        config_path=tmp_path / "rclone.conf",
        filter_path=tmp_path / "filters.txt",
        workdir_base=tmp_path / "bisync",
        version_root=tmp_path / "versions",
        dry_run=True,
        stamp="20261012-100000",
    )[0]
    live = build_commands(
        job,
        shares_root=root,
        config_path=tmp_path / "rclone.conf",
        filter_path=tmp_path / "filters.txt",
        workdir_base=tmp_path / "bisync",
        version_root=tmp_path / "versions",
        stamp="20261012-100000",
    )[0]
    assert "--dry-run" in dry and "--use-json-log" in dry
    assert dry[dry.index("--log-level") + 1] == "DEBUG"
    assert "--use-json-log" not in live
    assert live[live.index("--log-level") + 1] == "NOTICE"


def test_dry_run_table_copy_overwrite_delete():
    text = """
# PLAN {"local":"photos","remote":"Medien/Fotos","dir":"up","version":false}
{"level":"debug","msg":"Destination doesn't exist","object":"2024/a.jpg"}
{"level":"notice","msg":"Skipped copy as --dry-run is set (size 12)","object":"2024/a.jpg"}
{"level":"notice","msg":"Skipped copy as --dry-run is set (size 20)","object":"b.jpg"}
{"level":"notice","msg":"Unchanged skipping","object":"gleich.jpg"}
{"level":"notice","msg":"Skipped delete as --dry-run is set (size 4)","object":"old.jpg"}
{"level":"notice","msg":"Skipped set directory modification time","object":"2024"}
"""
    rows, total = parse_dry_run(text)
    assert total == 3
    assert rows[0]["local"] == "photos/2024/a.jpg"
    assert rows[0]["remote"] == "Medien/Fotos/2024/a.jpg"
    assert rows[0]["kind"] == "copy"
    assert "Unraid → kDrive" in rows[0]["label"]
    assert rows[1]["kind"] == "overwrite"
    assert rows[1]["local"] == "photos/b.jpg"
    assert rows[2]["kind"] == "delete"
    assert rows[2]["local"] == ""
    assert rows[2]["remote"] == "Medien/Fotos/old.jpg"
    assert "Löschen auf kDrive" in rows[2]["label"]
    assert plan_summary(rows) == "Probelauf: 1 kopieren, 1 überschreiben, 1 löschen"


def test_dry_run_download_and_bisync_direction():
    text = """
# PLAN {"local":"docs","remote":"Archiv","dir":"down","version":true}
2026/10/12 12:00:00 DEBUG : neu.txt: Destination doesn't exist
2026/10/12 12:00:00 NOTICE: neu.txt: Skipped copy as --dry-run is set (size 1)
2026/10/12 12:00:01 NOTICE: alt.txt: Skipped copy as --dry-run is set (size 2)
# PLAN {"local":"photos","remote":"Fotos","dir":"both","version":false}
{"level":"info","msg":"Copying Unique Path2 files to Path1"}
{"level":"notice","msg":"Skipped copy as --dry-run is set (size 3)","object":"von-kdrive.txt"}
{"level":"info","msg":"Deleting on path1"}
{"level":"notice","msg":"Skipped delete as --dry-run is set (size 4)","object":"weg.txt"}
"""
    rows, total = parse_dry_run(text)
    assert total == 4
    assert rows[0]["kind"] == "copy"
    assert rows[0]["local"] == "docs/neu.txt"
    assert rows[0]["remote"] == "Archiv/neu.txt"
    assert "kDrive → Unraid" in rows[0]["label"]
    assert "Versionsordner" in rows[1]["label"]
    assert rows[2]["kind"] == "overwrite"
    assert "kDrive → Unraid" in rows[2]["label"]
    assert rows[3]["kind"] == "delete"
    assert rows[3]["local"] == "photos/weg.txt"
    assert rows[3]["remote"] == ""
    assert "Löschen auf Unraid" in rows[3]["label"]
