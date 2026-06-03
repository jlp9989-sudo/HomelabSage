"""Tests for the CSV history exporter."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from pathlib import Path

from homelabsage.db import Database
from homelabsage.history import COLUMNS, dump_to_string, write_csv
from homelabsage.models import (
    Analysis,
    AnalyzedUpdate,
    Severity,
    Update,
    UpdateStatus,
)


def _seed(db: Database) -> None:
    items = [
        AnalyzedUpdate(
            update=Update(
                source="docker", subject="mealie",
                current_version="1.0", new_version="2.0",
                release_url="https://example/mealie",
            ),
            analysis=Analysis(
                severity=Severity.HIGH, summary="Major upgrade",
                breaking_changes=["env: A → B", "data dir moved"],
                recommended_action="back up DB",
            ),
            status=UpdateStatus.ANALYZED,
            detected_at=datetime(2026, 6, 1, 12, tzinfo=UTC),
            analyzed_at=datetime(2026, 6, 1, 12, 5, tzinfo=UTC),
        ),
        AnalyzedUpdate(
            update=Update(source="docker", subject="boring",
                          current_version="3", new_version="4"),
            status=UpdateStatus.NEW,
            detected_at=datetime(2026, 6, 2, tzinfo=UTC),
        ),
    ]
    for it in items:
        db.upsert(it)


def test_dump_to_string_has_header_and_rows(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        _seed(db)
        csv_text = dump_to_string(db)
    finally:
        db.close()
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    assert reader.fieldnames == list(COLUMNS)
    assert len(rows) == 2
    mealie = next(r for r in rows if r["subject"] == "mealie")
    assert mealie["severity"] == "high"
    assert mealie["status"] == "analyzed"
    assert mealie["breaking_changes"] == "env: A → B | data dir moved"
    assert mealie["recommended_action"] == "back up DB"
    assert mealie["release_url"] == "https://example/mealie"


def test_write_csv_to_file(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    out = tmp_path / "history.csv"
    try:
        _seed(db)
        n = write_csv(db, out)
    finally:
        db.close()
    assert n == 2
    body = out.read_text(encoding="utf-8")
    assert "mealie" in body
    assert "boring" in body
    assert body.startswith(",".join(COLUMNS)) or "id,source,subject" in body.splitlines()[0]


def test_dump_to_string_no_analysis_emits_empty_cells(tmp_path: Path):
    """Items without analysis (`status=new`) should still appear with
    blank severity / summary / breaking_changes / recommended_action."""
    db = Database(tmp_path / "s.sqlite")
    try:
        _seed(db)
        text = dump_to_string(db)
    finally:
        db.close()
    rows = list(csv.DictReader(io.StringIO(text)))
    boring = next(r for r in rows if r["subject"] == "boring")
    assert boring["severity"] == ""
    assert boring["summary"] == ""
    assert boring["breaking_changes"] == ""


def test_write_csv_creates_parent_dir(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    out = tmp_path / "nested" / "deeper" / "history.csv"
    try:
        _seed(db)
        n = write_csv(db, out)
    finally:
        db.close()
    assert n == 2
    assert out.exists()
