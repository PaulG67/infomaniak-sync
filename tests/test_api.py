from pathlib import Path

import pytest

from app.main import create_app


@pytest.fixture
def env(tmp_path, monkeypatch):
    config = tmp_path / "config"
    shares = tmp_path / "shares"
    (shares / "photos" / "2024").mkdir(parents=True)
    (shares / "docs").mkdir()
    monkeypatch.setenv("CONFIG_DIR", str(config))
    monkeypatch.setenv("SHARES_ROOT", str(shares))
    monkeypatch.setenv("SYNC_SCHEDULER", "0")
    monkeypatch.setenv("WEB_PASSWORD", "")
    monkeypatch.setenv("WEB_USER", "admin")
    monkeypatch.setenv("KDRIVE_ID", "")
    monkeypatch.setenv("KDRIVE_USER", "")
    monkeypatch.setenv("KDRIVE_PASSWORD", "")
    monkeypatch.setenv("RCLONE_REMOTE", "")
    monkeypatch.setenv("RCLONE_CONFIG_FILE", str(tmp_path / "missing-rclone.conf"))
    monkeypatch.setenv("TZ", "Europe/Zurich")
    return {"config": config, "shares": shares}


def test_health_and_create_job(env):
    client = create_app().test_client()
    health = client.get("/health")
    assert health.status_code == 200
    assert health.get_json()["version"] == "1.2.1"
    page = client.get("/")
    html = page.get_data(as_text=True)
    assert "Version 1.2.1" in html
    assert "app.js?v=1.2.1" in html
    assert "Einstellungen" in html
    assert 'data-tab="settings"' in html
    assert 'data-tab="connection"' not in html
    assert page.headers["Cache-Control"] == "no-store"
    state = client.get("/api/state").get_json()
    assert state["share_count"] == 2
    assert state["app_version"] == "1.2.1"
    assert "synology_ok" in state
    assert "synology" in state
    assert "shares" in state["synology"]
    assert "scripts_available" not in state
    assert state["blank"]["mode"] == "upload"
    created = client.post(
        "/api/jobs",
        json={
            "name": "Fotos",
            "enabled": True,
            "mappings": [{"local": "photos", "remote": "Medien/Fotos"}],
            "mode": "upload",
            "existing": "newer",
            "schedule": {"type": "daily", "time": "03:30"},
        },
    )
    assert created.status_code == 200, created.get_json()
    job = created.get_json()["job"]
    assert job["mappings"][0]["remote"] == "Medien/Fotos"
    assert job["next_run"]
    assert "Medien/Fotos" in job["description"]
    exported = client.get(f"/api/jobs/{job['id']}/script")
    assert exported.status_code == 200, exported.get_json()
    body = exported.get_json()
    assert body["filename"].endswith(".sh")
    assert body["script"].startswith("#!/bin/bash")
    assert "Medien/Fotos" in body["script"]
    assert ".oc-nodes" not in body["script"]
    missing = client.post("/api/jobs", json={"name": "X", "mappings": [{"local": "photos/../../etc", "remote": "x"}]})
    assert missing.status_code == 400
    mirror = client.post("/api/jobs", json={"name": "Spiegel", "mode": "mirror_down", "mappings": [{"local": "docs", "remote": "Docs"}]})
    assert mirror.status_code == 400
    preview = client.post("/api/preview", json={"job": {"name": "Fotos", "mappings": [{"local": "photos", "remote": "Fotos"}]}})
    assert preview.status_code == 200
    assert "rclone copy" in preview.get_json()["commands"][0]


def test_connection_env_does_not_override_ui(env, monkeypatch):
    monkeypatch.setenv("KDRIVE_ID", "123456")
    monkeypatch.setenv("KDRIVE_USER", "ada@example.com")
    monkeypatch.setenv("KDRIVE_PASSWORD", "env-secret")
    client = create_app().test_client()
    state = client.get("/api/state").get_json()
    assert state["connection"]["source"] == "env"
    assert state["connection"]["kdrive_id"] == "123456"
    assert "env-secret" not in (env["config"] / "rclone.conf").read_text(encoding="utf-8")
    assert (env["config"] / "kdrive.password").read_text(encoding="utf-8") == "env-secret"
    blob = b"".join(path.read_bytes() for path in env["config"].glob("app.db*"))
    assert b"env-secret" not in blob

    saved = client.post(
        "/api/connection",
        json={"kdrive_id": "123456", "kdrive_user": "ada@example.com", "password": "ui-secret"},
    )
    assert saved.status_code == 200
    assert saved.get_json()["connection"]["source"] == "ui"

    monkeypatch.setenv("KDRIVE_PASSWORD", "other-secret")
    again = create_app().test_client()
    assert (env["config"] / "kdrive.password").read_text(encoding="utf-8") == "ui-secret"
    reset = again.post("/api/connection/reset-env")
    assert reset.status_code == 200
    assert (env["config"] / "kdrive.password").read_text(encoding="utf-8") == "other-secret"
    bad = again.post("/api/connection", json={"kdrive_id": "nope", "kdrive_user": "ada@example.com", "password": "x"})
    assert bad.status_code == 400


def test_login_gate(env, monkeypatch):
    monkeypatch.setenv("WEB_PASSWORD", "geheim")
    client = create_app().test_client()
    assert client.get("/health").status_code == 200
    assert client.get("/api/state").status_code == 401
    assert client.post("/login", data={"user": "admin", "password": "no"}).status_code == 401
    assert client.post("/login", data={"user": "admin", "password": "geheim"}).status_code == 302
    assert client.get("/api/state").status_code == 200


def test_rclone_file_is_selectable_without_second_password(env, monkeypatch):
    conf = env["shares"].parent / "plugin.conf"
    conf.write_text(
        "\n".join(
            [
                "[Infomaniak]",
                "type = webdav",
                "url = https://123456.connect.kdrive.infomaniak.com",
                "vendor = other",
                "user = ada@example.com",
                "pass = SECRET-ONE",
                "",
                "[Anderer]",
                "type = webdav",
                "url = https://999.connect.kdrive.infomaniak.com",
                "user = bob@example.com",
                "pass = SECRET-TWO",
                "",
                "[Lokal]",
                "type = local",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RCLONE_CONFIG_FILE", str(conf))
    monkeypatch.setenv("RCLONE_REMOTE", "Infomaniak")
    monkeypatch.setenv("KDRIVE_ID", "777")
    monkeypatch.setenv("KDRIVE_USER", "other@example.com")
    monkeypatch.setenv("KDRIVE_PASSWORD", "other-secret")
    client = create_app().test_client()
    state = client.get("/api/state").get_json()
    assert state["connection"]["mode"] == "rclone"
    assert state["connection"]["ready"] is True
    assert state["connection"]["rclone_remote"] == "Infomaniak"
    assert [item["name"] for item in state["connection"]["remotes"]] == ["Infomaniak", "Anderer"]
    blob = str(state)
    assert "SECRET-ONE" not in blob
    assert "SECRET-TWO" not in blob
    assert "other-secret" not in blob
    assert not (env["config"] / "kdrive.password").exists()
    preview = client.post("/api/preview", json={"job": {"name": "Fotos", "mappings": [{"local": "photos", "remote": "Fotos"}]}})
    command = preview.get_json()["commands"][0]
    assert "Infomaniak:Fotos" in command
    active = (env["config"] / "rclone-active.conf").read_text(encoding="utf-8")
    assert "vendor = other" in active
    assert "url = https://123456.connect.kdrive.infomaniak.com" in active
    assert "pass = SECRET-ONE" in active
    assert str(conf) not in command
    assert "other-secret" not in command
    assert "SECRET-ONE" not in blob
    switched = client.post("/api/connection", json={"mode": "rclone", "rclone_remote": "Anderer"})
    assert switched.status_code == 200
    assert switched.get_json()["connection"]["rclone_remote"] == "Anderer"
    assert switched.get_json()["connection"]["user"] == "bob@example.com"
    missing = client.post("/api/connection", json={"mode": "rclone", "rclone_remote": "Lokal"})
    assert missing.status_code == 400
    manual = client.post(
        "/api/connection",
        json={"mode": "manual", "kdrive_id": "123456", "kdrive_user": "ada@example.com", "password": "ui-secret"},
    )
    assert manual.status_code == 200
    again = create_app().test_client()
    assert again.get("/api/state").get_json()["connection"]["mode"] == "manual"
    reset = again.post("/api/connection/reset-env")
    assert reset.status_code == 200
    body = reset.get_json()["connection"]
    assert body["mode"] == "rclone"
    assert body["rclone_remote"] == "Infomaniak"
    assert "ui-secret" not in str(body)


def test_manual_connection_accepts_rclone_url_and_obscured_pass(env):
    client = create_app().test_client()
    saved = client.post(
        "/api/connection",
        json={
            "mode": "manual",
            "webdav_url": "https://260259.connect.kdrive.infomaniak.com",
            "kdrive_user": "paul@gehring.li",
            "password": "OBSCURED-PASS",
            "password_obscured": True,
        },
    )
    assert saved.status_code == 200, saved.get_json()
    body = saved.get_json()["connection"]
    assert body["kdrive_id"] == "260259"
    assert body["kdrive_user"] == "paul@gehring.li"
    assert body["url"] == "https://260259.connect.kdrive.infomaniak.com"
    assert body["password_obscured"] is True
    assert body["ready"] is True
    assert "OBSCURED-PASS" not in str(body)
    conf = (env["config"] / "rclone.conf").read_text(encoding="utf-8")
    assert "type = webdav" in conf
    assert "vendor = other" in conf
    assert "url = https://260259.connect.kdrive.infomaniak.com" in conf
    assert "user = paul@gehring.li" in conf
    assert "pass = OBSCURED-PASS" in conf


def test_probe_rclone_reports_version_and_missing_binary(monkeypatch):
    from app.main import probe_rclone

    class Proc:
        stdout = "rclone v1.75.2\n- os/version: test\n"
        stderr = ""
        returncode = 0

    monkeypatch.setattr("app.main.rclone_binary", lambda: "rclone")
    monkeypatch.setattr("app.main.subprocess.run", lambda *args, **kwargs: Proc())
    version, error = probe_rclone()
    assert version == "rclone v1.75.2"
    assert error == ""

    def missing(*args, **kwargs):
        raise FileNotFoundError("rclone")

    monkeypatch.setattr("app.main.subprocess.run", missing)
    version, error = probe_rclone()
    assert version is None
    assert "nicht vorhanden" in error


def test_run_requires_connection(env):
    client = create_app().test_client()
    created = client.post(
        "/api/jobs",
        json={"name": "Fotos", "mappings": [{"local": "photos", "remote": "Fotos"}]},
    )
    job_id = created.get_json()["job"]["id"]
    started = client.post(f"/api/jobs/{job_id}/run", json={})
    assert started.status_code == 400
    assert "nicht eingerichtet" in started.get_json()["error"]
