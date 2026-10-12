import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_unraid_template_has_required_fields():
    tree = ET.parse(ROOT / "unraid" / "my-infomaniak-sync.xml")
    assert tree.findtext("Repository") == "ghcr.io/paulg67/infomaniak-sync:latest"
    assert tree.findtext("Name") == "infomaniak-sync"
    names = [node.attrib.get("Name") for node in tree.findall("Config")]
    for required in ("Shares", "Config", "kDrive-ID", "kDrive-Benutzer", "kDrive-Passwort", "WebUI Port"):
        assert required in names
    password = next(node for node in tree.findall("Config") if node.attrib.get("Name") == "kDrive-Passwort")
    assert password.attrib.get("Mask") == "true"
    script = (ROOT / "unraid" / "install-template.sh").read_text(encoding="utf-8")
    assert "my-infomaniak-sync.xml" in script
    assert "set -euo pipefail" in script
