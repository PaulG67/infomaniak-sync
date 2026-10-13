import json

import pytest

from app.main import create_app
from app.userscripts import UserScripts


def _tree(tmp_path):
    root = tmp_path / "user.scripts"
    live = tmp_path / "live"
    scripts = root / "scripts"
    folder = scripts / "backup.daily"
    folder.mkdir(parents=True)
    (folder / "script").write_text("#!/bin/bash\n#name=Nachtbackup\n#description=Sichert Fotos\necho ok\n", encoding="utf-8")
    (folder / "name").write_text("Nachtbackup\n", encoding="utf-8")
    other = scripts / "leer"
    other.mkdir()
    (other / "script").write_text("#!/bin/bash\necho leer\n", encoding="utf-8")
    live.mkdir()
    return root, live


def test_list_save_schedule_and_reject_bad_cron(tmp_path):
    root, live = _tree(tmp_path)
    store = UserScripts(root, live)
    listed = store.list_scripts()
    assert [item["id"] for item in listed] == ["backup.daily", "leer"]
    assert listed[0]["name"] == "Nachtbackup"
    assert listed[0]["description"] == "Sichert Fotos"
    assert listed[0]["enabled"] is False

    saved = store.save_script("backup.daily", "#!/bin/bash\necho neu\n", enabled=True, frequency="daily", custom="")
    assert saved["enabled"] is True
    assert saved["frequency"] == "daily"
    assert "echo neu" in (root / "scripts" / "backup.daily" / "script").read_text(encoding="utf-8")
    schedule = json.loads((root / "schedule.json").read_text(encoding="utf-8"))
    entry = schedule["/boot/config/plugins/user.scripts/scripts/backup.daily/script"]
    assert entry["frequency"] == "daily"
    assert entry["id"] == "schedulebackup-daily"
    assert json.loads((live / "schedule.json").read_text(encoding="utf-8")) == schedule

    store.save_script("backup.daily", "#!/bin/bash\necho neu\n", enabled=True, frequency="custom", custom="15 4 * * 1")
    cron = (root / "customSchedule.cron").read_text(encoding="utf-8")
    assert "15 4 * * 1" in cron
    assert "/boot/config/plugins/user.scripts/scripts/backup.daily/script" in cron

    store.save_script("backup.daily", "#!/bin/bash\necho neu\n", enabled=False, frequency="daily", custom="")
    assert store.get_script("backup.daily")["frequency"] == "disabled"
    assert not (root / "customSchedule.cron").exists()

    with pytest.raises(ValueError):
        store.save_script("backup.daily", "#!/bin/bash\n", enabled=True, frequency="custom", custom="* * * * *; rm -rf /")
    with pytest.raises(ValueError):
        store.get_script("../etc")


def test_scripts_api(tmp_path, monkeypatch):
    root, live = _tree(tmp_path)
    config = tmp_path / "config"
    shares = tmp_path / "shares"
    shares.mkdir()
    monkeypatch.setenv("CONFIG_DIR", str(config))
    monkeypatch.setenv("SHARES_ROOT", str(shares))
    monkeypatch.setenv("SYNC_SCHEDULER", "0")
    monkeypatch.setenv("WEB_PASSWORD", "")
    monkeypatch.setenv("USER_SCRIPTS_DIR", str(root))
    monkeypatch.setenv("USER_SCRIPTS_LIVE", str(live))
    monkeypatch.setenv("RCLONE_CONFIG_FILE", str(tmp_path / "missing.conf"))
    client = create_app().test_client()
    listed = client.get("/api/scripts")
    assert listed.status_code == 200
    assert listed.get_json()["available"] is True
    assert len(listed.get_json()["scripts"]) == 2
    saved = client.put(
        "/api/scripts/leer",
        json={"script": "#!/bin/bash\necho hallo\n", "enabled": True, "frequency": "weekly", "custom": ""},
    )
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json()["script"]["frequency_label"] == "wöchentlich"
    missing = client.get("/api/scripts/nicht-da")
    assert missing.status_code == 404
