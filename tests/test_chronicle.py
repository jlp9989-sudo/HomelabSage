"""Tests for the server chronicle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from homelabsage.chronicle import (
    build_chronicle,
    render_markdown,
    run_chronicle,
    write_to_notes,
)
from homelabsage.db import Database
from homelabsage.models import (
    Analysis,
    AnalyzedUpdate,
    Severity,
    Update,
    UpdateStatus,
)


def _seed(db: Database, now: datetime) -> None:
    rows = [
        # applied — included
        ("docker", "openclaw", Severity.HIGH,
         UpdateStatus.APPLIED, now - timedelta(days=2), [], None),
        # dismissed — included
        ("docker", "tintes", Severity.MEDIUM,
         UpdateStatus.DISMISSED, now - timedelta(days=5), [], None),
        # breaking — included
        ("docker", "mealie", Severity.HIGH,
         UpdateStatus.ANALYZED, now - timedelta(days=7),
         ["env: OLD → NEW", "data dir moved"], None),
        # hold-recommended — included
        ("docker", "kavita", Severity.MEDIUM,
         UpdateStatus.ANALYZED, now - timedelta(days=10),
         [], "hold; upstream archived"),
        # plain analyzed without anything special — NOT included
        ("docker", "boring", Severity.INFO,
         UpdateStatus.ANALYZED, now - timedelta(days=3), [], None),
        # too old — NOT included
        ("docker", "ancient", Severity.CRITICAL,
         UpdateStatus.APPLIED, now - timedelta(days=60), [], None),
    ]
    for source, subject, sev, status, when, breaking, action in rows:
        item = AnalyzedUpdate(
            update=Update(source=source, subject=subject,
                          current_version="1.0", new_version="2.0",
                          context={}),
            analysis=Analysis(
                severity=sev, summary=f"{subject} summary",
                breaking_changes=breaking,
                recommended_action=action,
            ),
            status=status,
            detected_at=when,
            analyzed_at=when,
        )
        db.upsert(item)


def test_build_chronicle_includes_applied_dismissed_breaking_hold(tmp_path: Path):
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    db = Database(tmp_path / "s.sqlite")
    try:
        _seed(db, now)
        chronicle = build_chronicle(db, days=30, now=now)
    finally:
        db.close()
    by_kind = chronicle.counts_by_kind()
    assert by_kind.get("applied") == 1
    assert by_kind.get("dismissed") == 1
    assert by_kind.get("breaking") == 1
    assert by_kind.get("hold") == 1
    # boring + ancient excluded
    assert "boring" not in [e.subject for e in chronicle.entries]
    assert "ancient" not in [e.subject for e in chronicle.entries]


def test_build_chronicle_detects_severity_jump(tmp_path: Path):
    """Same subject, two analyses, severity went up → severity_jump entry."""
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    db = Database(tmp_path / "s.sqlite")
    try:
        # Two rows for the same subject (different new_versions so they
        # don't dedup) — first row info, second row critical
        for nv, sev in (("2.0", Severity.INFO), ("3.0", Severity.CRITICAL)):
            db.upsert(AnalyzedUpdate(
                update=Update(source="docker", subject="climber",
                              current_version="1.0", new_version=nv),
                analysis=Analysis(severity=sev, summary="—"),
                status=UpdateStatus.ANALYZED,
                detected_at=now - timedelta(days=1 if nv == "3.0" else 10),
                analyzed_at=now - timedelta(days=1 if nv == "3.0" else 10),
            ))
        chronicle = build_chronicle(db, days=30, now=now)
    finally:
        db.close()
    jumps = [e for e in chronicle.entries if e.kind == "severity_jump"]
    assert len(jumps) == 1
    assert "info" in jumps[0].headline and "critical" in jumps[0].headline


def test_build_chronicle_empty_when_only_recent_boring(tmp_path: Path):
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    db = Database(tmp_path / "s.sqlite")
    try:
        db.upsert(AnalyzedUpdate(
            update=Update(source="docker", subject="x",
                          current_version="1", new_version="2"),
            analysis=Analysis(severity=Severity.INFO, summary="—"),
            status=UpdateStatus.ANALYZED,
            detected_at=now,
        ))
        chronicle = build_chronicle(db, days=30, now=now)
    finally:
        db.close()
    assert chronicle.entries == []


def test_build_chronicle_handles_naive_now_arg(tmp_path: Path):
    """Caller can pass a naive datetime — chronicle upgrades to tz-aware UTC."""
    naive = datetime(2026, 6, 1, 12, 0, 0)
    db = Database(tmp_path / "s.sqlite")
    try:
        chronicle = build_chronicle(db, days=7, now=naive)
    finally:
        db.close()
    # No crash; window is computed even with no data
    assert chronicle.period_end.tzinfo is not None


def test_render_markdown_empty_says_quiet():
    from homelabsage.chronicle import Chronicle
    c = Chronicle(
        period_start=datetime(2026, 5, 1, tzinfo=UTC),
        period_end=datetime(2026, 6, 1, tzinfo=UTC),
    )
    body = render_markdown(c)
    assert "Nothing of note" in body


def test_render_markdown_sections_appear_when_populated(tmp_path: Path):
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    db = Database(tmp_path / "s.sqlite")
    try:
        _seed(db, now)
        chronicle = build_chronicle(db, days=30, now=now)
    finally:
        db.close()
    body = render_markdown(chronicle)
    assert "# Homelab chronicle" in body
    assert "## Applied" in body
    assert "## Dismissed" in body
    assert "## Breaking changes flagged" in body
    assert "## Hold-recommended" in body
    assert "openclaw" in body
    assert "mealie" in body


def test_write_to_notes_atomic(tmp_path: Path):
    out = write_to_notes(tmp_path / "notes", "# chronicle")
    assert out and out.exists()
    assert out.read_text().startswith("# chronicle")


def test_write_to_notes_noop_when_empty():
    assert write_to_notes("", "x") is None


def test_run_chronicle_writes_when_notes_dir_configured(tmp_path: Path):
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    db = Database(tmp_path / "s.sqlite")
    try:
        _seed(db, now)
        chronicle, path = run_chronicle(db, str(tmp_path / "notes"), days=30)
    finally:
        db.close()
    assert path == tmp_path / "notes" / "chronicle.md"
    assert len(chronicle.entries) > 0
    assert "Homelab chronicle" in path.read_text()


def test_run_chronicle_skips_write_when_no_notes_dir(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        chronicle, path = run_chronicle(db, None, days=30)
    finally:
        db.close()
    assert path is None
