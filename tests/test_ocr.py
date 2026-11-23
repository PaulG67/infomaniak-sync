from pathlib import Path

import pytest

from app.engine import blank_job, describe, prepare_job
from app.ocr import list_pdfs, normalize_languages, ocr_one, process_roots
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Zurich")
NOW = datetime(2026, 10, 12, 10, 0, tzinfo=TZ)


def test_normalize_languages():
    assert normalize_languages("deu+eng") == "deu+eng"
    assert normalize_languages(" DEU ") == "deu"
    with pytest.raises(ValueError):
        normalize_languages("deutsch")


def test_list_pdfs(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "B.PDF").write_bytes(b"%PDF")
    (tmp_path / "skip.txt").write_text("x", encoding="utf-8")
    names = [p.name.lower() for p in list_pdfs(tmp_path)]
    assert names == ["a.pdf", "b.pdf"]


def test_ocr_one_dry_and_skip(tmp_path, monkeypatch):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    class Proc:
        def __init__(self, code):
            self.returncode = code
            self.stderr = ""
            self.stdout = ""

    monkeypatch.setattr("app.ocr.subprocess.run", lambda *a, **k: Proc(6))
    assert ocr_one(pdf, languages="deu+eng", binary="ocrmypdf", dry_run=False) == "skipped"
    assert ocr_one(pdf, languages="deu+eng", binary="ocrmypdf", dry_run=True) == "would_process"


def test_ocr_preserves_mtime_so_min_age_still_applies(tmp_path, monkeypatch):
    import os
    import time

    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    old = time.time() - 3600
    os.utime(pdf, (old, old))

    class Proc:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(argv, **kwargs):
        out = Path(argv[-1])
        out.write_bytes(b"%PDF-ocr")
        return Proc()

    monkeypatch.setattr("app.ocr.subprocess.run", fake_run)
    assert ocr_one(pdf, languages="deu", binary="ocrmypdf") == "processed"
    assert pdf.read_bytes() == b"%PDF-ocr"
    assert abs(pdf.stat().st_mtime - old) < 2


def test_process_roots_dry_run(tmp_path, monkeypatch):
    (tmp_path / "x.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr("app.ocr.ocrmypdf_binary", lambda: None)
    result = process_roots([tmp_path], dry_run=True)
    assert result["would"] == 1
    assert result["processed"] == 0


def test_prepare_job_stores_ocr(tmp_path):
    root = tmp_path / "shares"
    (root / "photos").mkdir(parents=True)
    job = prepare_job(
        {
            **blank_job(),
            "name": "OCR",
            "mappings": [{"local": "photos", "remote": "Out"}],
            "ocr_pdf": True,
            "ocr_languages": "deu",
        },
        old=None,
        shares_root=root,
        now=NOW,
        tz=TZ,
    )
    assert job["ocr_pdf"] is True
    assert job["ocr_languages"] == "deu"
    assert "PDF-OCR aktiv" in describe(job)
