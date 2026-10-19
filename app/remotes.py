"""Unraid Unassigned Devices: SMB-/NFS-Remotes lesen."""

from __future__ import annotations

import os
import re
from pathlib import Path

SECTION = re.compile(r"^\[(.+)\]\s*$")
KV = re.compile(r'^([A-Za-z0-9_]+)\s*=\s*"(.*)"\s*$')
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,120}$")

HOST_REMOTES = "/mnt/remotes"
UD_EXAMPLE = "/boot/config/plugins/unassigned.devices"


def _strip_quotes(value: str) -> str:
    return str(value or "").strip().strip('"').strip("'")


def _mount_leaf(mountpoint: str, share: str, source: str) -> str:
    mountpoint = (mountpoint or "").replace("\\", "/").rstrip("/")
    if mountpoint:
        leaf = Path(mountpoint).name
        if leaf and leaf not in {".", ".."}:
            return leaf
    share = (share or "").strip().strip("/")
    if share and "/" not in share and "\\" not in share:
        return share
    # //HOST/SHARE → HOST_SHARE
    src = (source or "").replace("\\", "/").strip("/")
    if src.startswith("//"):
        parts = [p for p in src[2:].split("/") if p]
        if len(parts) >= 2:
            return f"{parts[0]}_{parts[1]}"
        if parts:
            return parts[0]
    return share or "remote"


def parse_samba_mount_cfg(text: str) -> list[dict]:
    """Parst samba_mount.cfg von Unassigned Devices."""
    current: dict | None = None
    found: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        section = SECTION.match(line)
        if section:
            if current:
                found.append(current)
            source = section.group(1).strip()
            current = {
                "source": source,
                "protocol": "",
                "ip": "",
                "path": "",
                "share": "",
                "mountpoint": "",
                "automount": "",
            }
            continue
        if current is None:
            continue
        match = KV.match(line)
        if not match:
            if "=" in line:
                key, value = line.split("=", 1)
                current[key.strip().lower()] = _strip_quotes(value)
            continue
        current[match.group(1).lower()] = match.group(2)
    if current:
        found.append(current)
    remotes = []
    for item in found:
        protocol = (item.get("protocol") or "").upper() or "SMB"
        source = item.get("source") or ""
        share = item.get("share") or item.get("path") or ""
        mountpoint = item.get("mountpoint") or ""
        leaf = _mount_leaf(mountpoint, share, source)
        if not SAFE_NAME.fullmatch(leaf):
            leaf = re.sub(r"[^A-Za-z0-9._@+-]+", "_", leaf).strip("._-")[:120] or "remote"
        host_mount = mountpoint.replace("\\", "/") if mountpoint else f"{HOST_REMOTES}/{leaf}"
        if not host_mount.startswith("/"):
            host_mount = f"{HOST_REMOTES}/{leaf}"
        ip = item.get("ip") or ""
        path = item.get("path") or share
        if source.startswith("//") and not ip:
            bits = [p for p in source[2:].split("/") if p]
            if bits:
                ip = bits[0]
                if len(bits) > 1 and not path:
                    path = "/".join(bits[1:])
        remotes.append(
            {
                "id": leaf,
                "source": source or (f"//{ip}/{path}" if ip else leaf),
                "protocol": protocol,
                "host": ip,
                "share": path or share or leaf,
                "mountpoint": host_mount,
                "automount": str(item.get("automount") or "").lower() in {"yes", "true", "1"},
            }
        )
    return remotes


def read_ud_remotes(config_dir: Path) -> list[dict]:
    path = config_dir / "samba_mount.cfg"
    if not path.is_file():
        # ältere/alternative Schreibweise
        path = config_dir / "samba_mounts.cfg"
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")[:500_000]
    except OSError:
        return []
    return parse_samba_mount_cfg(text)


def _mounts_from_proc() -> set[str]:
    mounts: set[str] = set()
    try:
        text = Path("/proc/mounts").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return mounts
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        mounts.add(parts[1])
    return mounts


def is_path_mounted(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if not resolved.is_dir():
        return False
    try:
        if resolved.is_mount():
            return True
    except OSError:
        pass
    as_posix = resolved.as_posix()
    return as_posix in _mounts_from_proc()


def list_synology_shares(
    *,
    ud_config_dir: Path,
    remotes_root: Path,
    selected_id: str = "",
) -> dict:
    """SMB-Remotes aus UD plus sichtbare Ordner unter dem Synology-Mount."""
    configured = read_ud_remotes(ud_config_dir)
    by_id = {item["id"]: dict(item) for item in configured}
    if remotes_root.is_dir():
        try:
            entries = sorted(remotes_root.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            entries = []
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if not SAFE_NAME.fullmatch(entry.name):
                continue
            if entry.name in by_id:
                continue
            by_id[entry.name] = {
                "id": entry.name,
                "source": entry.name,
                "protocol": "SMB",
                "host": "",
                "share": entry.name,
                "mountpoint": f"{HOST_REMOTES}/{entry.name}",
                "automount": False,
                "from_config": False,
            }
    shares = []
    for item in sorted(by_id.values(), key=lambda row: row["id"].lower()):
        leaf = item["id"]
        container_path = remotes_root / leaf
        shares.append(
            {
                **item,
                "from_config": bool(item.get("from_config", "from_config" not in item)),
                "mounted": is_path_mounted(container_path),
                "container_path": str(container_path),
                "selected": leaf == selected_id,
            }
        )
    available = ud_config_dir.is_dir()
    return {
        "available": available or remotes_root.is_dir(),
        "ud_present": available,
        "ud_path": str(ud_config_dir),
        "remotes_root": str(remotes_root),
        "selected": selected_id,
        "shares": shares,
        "example_ud": UD_EXAMPLE,
        "example_remotes": HOST_REMOTES,
    }


def resolve_synology_root(remotes_root: Path, selected_id: str) -> Path:
    selected_id = (selected_id or "").strip()
    if not selected_id:
        return remotes_root
    if not SAFE_NAME.fullmatch(selected_id):
        raise ValueError("Ungültiger Synology-Share")
    target = (remotes_root / selected_id).resolve()
    try:
        target.relative_to(remotes_root.resolve())
    except ValueError as exc:
        raise ValueError("Synology-Share liegt ausserhalb des Remotes-Pfads") from exc
    return target
