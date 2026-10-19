from pathlib import Path

from app.remotes import list_synology_shares, parse_samba_mount_cfg, resolve_synology_root


SAMPLE = """
[//synology/backup]
protocol = "SMB"
ip = "synology"
path = "backup"
share = "backup"
automount = "yes"
mountpoint = "/mnt/remotes/synology_backup"

[//nas/media]
protocol = "SMB"
ip = "nas"
path = "media"
share = "media"
automount = "no"
"""


def test_parse_samba_mount_cfg():
    remotes = parse_samba_mount_cfg(SAMPLE)
    assert len(remotes) == 2
    first = remotes[0]
    assert first["id"] == "synology_backup"
    assert first["source"] == "//synology/backup"
    assert first["mountpoint"] == "/mnt/remotes/synology_backup"
    assert first["host"] == "synology"
    assert first["share"] == "backup"
    assert remotes[1]["id"] == "nas_media"
    assert remotes[1]["mountpoint"] == "/mnt/remotes/nas_media"


def test_parse_unraid_style_ip_share_names():
    text = """
[//192.168.0.30/backup-unraid]
protocol="SMB"
ip="192.168.0.30"
path="backup-unraid"
share="backup-unraid"

[//DS918PLUS.HOME/Familien_Dokumente]
protocol = "SMB"
ip = "DS918PLUS.HOME"
path = "Familien_Dokumente"
share = "Familien_Dokumente"
"""
    remotes = parse_samba_mount_cfg(text)
    assert remotes[0]["id"] == "192.168.0.30_backup-unraid"
    assert remotes[0]["source"] == "//192.168.0.30/backup-unraid"
    assert remotes[1]["id"] == "DS918PLUS.HOME_Familien_Dokumente"


def test_list_synology_shares_marks_mount_and_selection(tmp_path):
    ud = tmp_path / "ud"
    ud.mkdir()
    # Unraid speichert oft UTF-8 mit BOM
    (ud / "samba_mount.cfg").write_text("\ufeff" + SAMPLE, encoding="utf-8")
    remotes = tmp_path / "remotes"
    mounted = remotes / "synology_backup"
    mounted.mkdir(parents=True)
    (mounted / "file.txt").write_text("x", encoding="utf-8")
    (remotes / "media").mkdir()
    data = list_synology_shares(ud_config_dir=ud, remotes_root=remotes, selected_id="synology_backup")
    assert data["ud_present"] is True
    assert data["ud_cfg_present"] is True
    assert data["remotes_ok"] is True
    by_id = {item["id"]: item for item in data["shares"]}
    assert by_id["synology_backup"]["selected"] is True
    assert by_id["synology_backup"]["source"] == "//synology/backup"
    assert by_id["synology_backup"]["mountpoint"] == "/mnt/remotes/synology_backup"
    assert by_id["nas_media"]["from_config"] is True
    assert by_id["media"]["from_config"] is False
    assert resolve_synology_root(remotes, "synology_backup") == mounted.resolve()
