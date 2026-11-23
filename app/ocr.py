"""PDF-OCR vor dem Sync: durchsuchbare Textschicht mit ocrmypdf/Tesseract."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("app.ocr")

LANG_OK = re.compile(r"^[a-z]{3}(\+[a-z]{3}){0,8}$")
DEFAULT_LANGUAGES = "deu+eng"


def normalize_languages(value: str | None) -> str:
    raw = str(value or "").strip().lower().replace(" ", "") or DEFAULT_LANGUAGES
    if not LANG_OK.fullmatch(raw):
        raise ValueError(
            "OCR-Sprachen ungültig. Beispiel: deu+eng (Tesseract-Codes, mit + verbunden)."
        )
    return raw


def ocrmypdf_binary() -> str | None:
    return shutil.which("ocrmypdf")


def require_ocrmypdf() -> str:
    path = ocrmypdf_binary()
    if not path:
        raise ValueError(
            "OCR ist aktiv, aber ocrmypdf fehlt im Container. Image aktualisieren (Force Update)."
        )
    return path


def list_pdfs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    found: list[Path] = []
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() == ".pdf":
            found.append(path)
    return sorted(found)


def ocr_one(pdf: Path, *, languages: str, binary: str, dry_run: bool = False) -> str:
    """OCR einer PDF. Rückgabe: processed | skipped | failed:<msg>.

    Die ursprüngliche Änderungszeit bleibt erhalten, damit --min-age und --update
    nach dem OCR nicht fälschlich greifen (sonst wirkt die Datei „brandneu“).
    """
    languages = normalize_languages(languages)
    if dry_run:
        return "would_process"
    tmp_path: Path | None = None
    try:
        stat = pdf.stat()
        original_mtime = stat.st_mtime
        original_atime = stat.st_atime
        with tempfile.NamedTemporaryFile(
            prefix=f".{pdf.stem}-ocr-",
            suffix=".pdf",
            dir=str(pdf.parent),
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
        proc = subprocess.run(
            [
                binary,
                "--skip-text",
                "--optimize",
                "1",
                "--output-type",
                "pdf",
                "-l",
                languages,
                str(pdf),
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        # 0 = ok, 6 = already has text (--skip-text) → leave original
        if proc.returncode == 6:
            if tmp_path and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            return "skipped"
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "ocrmypdf fehlgeschlagen").strip()
            if tmp_path and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            return f"failed:{err[:300]}"
        tmp_path.replace(pdf)
        try:
            os.utime(pdf, (original_atime, original_mtime))
        except OSError as exc:
            log.warning("mtime nach OCR nicht wiederhergestellt für %s: %s", pdf, exc)
        return "processed"
    except subprocess.TimeoutExpired:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        return "failed:OCR-Zeit überschritten (30 Min.)"
    except OSError as exc:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        return f"failed:{exc}"


def process_roots(
    roots: list[Path],
    *,
    languages: str = DEFAULT_LANGUAGES,
    dry_run: bool = False,
    cancel_check=None,
) -> dict:
    """OCR über mehrere Quellordner. cancel_check() → True bricht ab."""
    languages = normalize_languages(languages)
    binary = require_ocrmypdf() if not dry_run else (ocrmypdf_binary() or "ocrmypdf")
    processed = skipped = failed = would = 0
    lines: list[str] = []
    seen: set[Path] = set()
    for root in roots:
        for pdf in list_pdfs(root):
            key = pdf.resolve()
            if key in seen:
                continue
            seen.add(key)
            if cancel_check and cancel_check():
                lines.append("OCR abgebrochen")
                break
            result = ocr_one(pdf, languages=languages, binary=binary, dry_run=dry_run)
            rel = str(pdf)
            if result == "processed":
                processed += 1
                lines.append(f"OCR ok: {rel}")
            elif result == "skipped":
                skipped += 1
                lines.append(f"OCR übersprungen (bereits Text): {rel}")
            elif result == "would_process":
                would += 1
                lines.append(f"OCR geplant: {rel}")
            else:
                failed += 1
                detail = result.split(":", 1)[-1] if result.startswith("failed:") else result
                lines.append(f"OCR Fehler: {rel} · {detail}")
                log.warning("OCR fehlgeschlagen für %s: %s", pdf, detail)
    return {
        "processed": processed,
        "skipped": skipped,
        "failed": failed,
        "would": would,
        "total": processed + skipped + failed + would,
        "lines": lines,
        "languages": languages,
    }
