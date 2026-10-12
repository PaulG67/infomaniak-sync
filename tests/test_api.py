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
    monkeypatch.setenv("TZ", "Europe/Zurich")
    return {"config": config, "shares": shares}


def test_health_and_create_job(env):
    client = create_app().test_client()
    assert client.get("/health").status_code == 200
    state = client.get("/api/state").get_json()
    assert state["share_count"] == 2
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
