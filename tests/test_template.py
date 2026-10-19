import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_unraid_template_has_required_fields():
    tree = ET.parse(ROOT / "unraid" / "my-infomaniak-sync.xml")
    assert tree.findtext("Repository") == "ghcr.io/paulg67/infomaniak-sync:latest"
    assert tree.findtext("Name") == "infomaniak-sync"
    names = [node.attrib.get("Name") for node in tree.findall("Config")]
    for required in ("Shares", "Config", "Remotes", "Unassigned Devices", "rclone-Konfiguration", "rclone-Remote", "kDrive-ID", "kDrive-Benutzer", "kDrive-Passwort", "WebUI Port"):
        assert required in names
    remotes = next(node for node in tree.findall("Config") if node.attrib.get("Name") == "Remotes")
    assert remotes.attrib.get("Target") == "/synology"
    assert remotes.text == "/mnt/remotes"
    ud = next(node for node in tree.findall("Config") if node.attrib.get("Name") == "Unassigned Devices")
    assert ud.attrib.get("Target") == "/unassigned-devices"
    assert ud.attrib.get("Mode") == "ro"
    assert ud.attrib.get("Display") == "advanced"
    assert ud.text == "/boot/config/plugins/unassigned.devices"
    rclone = next(node for node in tree.findall("Config") if node.attrib.get("Name") == "rclone-Konfiguration")
    assert rclone.attrib.get("Target") == "/rclone/rclone.conf"
    assert rclone.attrib.get("Mode") == "ro"
    assert rclone.text == "/boot/config/plugins/rclone/.rclone.conf"
    assert "[Infomaniak]" in rclone.attrib.get("Description", "")
    remote = next(node for node in tree.findall("Config") if node.attrib.get("Name") == "rclone-Remote")
    assert remote.attrib.get("Target") == "RCLONE_REMOTE"
    assert remote.text == "Infomaniak"
    assert "User Scripts" not in names
    password = next(node for node in tree.findall("Config") if node.attrib.get("Name") == "kDrive-Passwort")
    assert password.attrib.get("Mask") == "true"
    script = (ROOT / "unraid" / "install-template.sh").read_text(encoding="utf-8")
    assert "my-infomaniak-sync.xml" in script
    assert "set -euo pipefail" in script
    assert "User Scripts" not in script
