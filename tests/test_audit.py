"""Tests for the proactive auditor."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from homelabsage import web
from homelabsage.audit import (
    build_report,
    render_markdown,
    run_audit,
    write_to_notes,
)
from homelabsage.config import Config
from homelabsage.db import Database
from homelabsage.models import (
    Analysis,
    AnalyzedUpdate,
    Severity,
    Update,
    UpdateStatus,
)


def _mk(
    subject: str,
    *,
    severity: Severity = Severity.HIGH,
    context: dict | None = None,
    status: UpdateStatus = UpdateStatus.ANALYZED,
) -> AnalyzedUpdate:
    return AnalyzedUpdate(
        update=Update(
            source="docker", subject=subject,
            current_version="1.0", new_version="2.0",
            context=context or {},
        ),
        analysis=Analysis(severity=severity, summary="—"),
        status=status,
        detected_at=datetime(2026, 6, 1, 12, 0, 0),
    )


# ─── per-signal extractors ──────────────────────────────────────────────


@pytest.fixture
def cfg_db(tmp_path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    db = Database(cfg.storage.database_path)
    yield cfg, db
    db.close()


def test_cve_critical_yields_critical_finding(cfg_db):
    cfg, db = cfg_db
    db.upsert(_mk("openclaw", context={
        "cve": {"counts": {"critical": 3, "high": 1},
                "top_critical": ["CVE-2026-0001", "CVE-2026-0002"]},
    }))
    report = build_report(cfg, db)
    crits = [f for f in report.findings if f.category == "cve"]
    assert len(crits) == 1
    assert crits[0].severity == "critical"
    assert "3 critical CVE" in crits[0].title
    assert "CVE-2026-0001" in crits[0].detail


def test_cve_high_only_falls_to_high(cfg_db):
    cfg, db = cfg_db
    db.upsert(_mk("x", context={
        "cve": {"counts": {"high": 2}, "top_high": ["CVE-9999"]},
    }))
    report = build_report(cfg, db)
    crits = [f for f in report.findings if f.category == "cve"]
    assert crits[0].severity == "high"


def test_abandoned_upstream_yields_high_finding(cfg_db):
    cfg, db = cfg_db
    db.upsert(_mk("ghost", context={
        "repo_health": {"status": "abandoned", "reason": "archived"},
    }))
    report = build_report(cfg, db)
    f = [x for x in report.findings if x.category == "abandoned"][0]
    assert f.severity == "high"
    assert "archived" in f.detail


def test_orphan_long_stopped_yields_medium_finding(cfg_db):
    cfg, db = cfg_db
    db.upsert(_mk("ghost", context={"orphan_since_days": 90}))
    report = build_report(cfg, db)
    f = [x for x in report.findings if x.category == "orphan"][0]
    assert f.severity == "medium"
    assert "90 days" in f.title


def test_orphan_under_30_days_no_finding(cfg_db):
    cfg, db = cfg_db
    db.upsert(_mk("ghost", context={"orphan_since_days": 5}))
    report = build_report(cfg, db)
    assert [x for x in report.findings if x.category == "orphan"] == []


def test_alternative_yields_info_finding_with_image_cite(cfg_db):
    cfg, db = cfg_db
    db.upsert(_mk("calibre-web", context={
        "alternatives": [{"image": "linuxserver/calibre-web", "github_url": "https://github.com/x/y"}],
    }))
    report = build_report(cfg, db)
    f = [x for x in report.findings if x.category == "alternative"][0]
    assert "linuxserver/calibre-web" in f.detail


def test_bloatware_image_growth_yields_info_finding(cfg_db):
    cfg, db = cfg_db
    db.upsert(_mk("fat", context={
        "image_size_growth": {"triggered": True, "ratio": 2.5,
                              "current_mib": 80, "new_mib": 200},
    }))
    report = build_report(cfg, db)
    f = [x for x in report.findings if x.category == "bloatware"][0]
    assert "2.5×" in f.title


def test_applied_and_dismissed_updates_excluded(cfg_db):
    cfg, db = cfg_db
    db.upsert(_mk("x", context={"orphan_since_days": 90}, status=UpdateStatus.APPLIED))
    db.upsert(_mk("y", context={"orphan_since_days": 90}, status=UpdateStatus.DISMISSED))
    report = build_report(cfg, db)
    # Both should be excluded — applied/dismissed are decisions already made
    assert report.findings == []
    assert report.healthy is True


def test_pending_dispatch_queue_yields_finding(cfg_db):
    cfg, db = cfg_db
    db.queue_pending_dispatch("docker:x:2", "telegram")
    db.queue_pending_dispatch("docker:y:2", "ntfy")
    report = build_report(cfg, db)
    f = [x for x in report.findings if x.category == "pending_push"][0]
    assert "2 push" in f.title
    assert "telegram" in f.detail and "ntfy" in f.detail


def test_findings_sorted_severity_first(cfg_db):
    cfg, db = cfg_db
    db.upsert(_mk("info_one", context={
        "alternatives": [{"image": "alt"}]}))
    db.upsert(_mk("critical_one", context={
        "cve": {"counts": {"critical": 1}, "top_critical": ["CVE-1"]}}))
    db.upsert(_mk("high_one", context={
        "repo_health": {"status": "abandoned"}}))
    report = build_report(cfg, db)
    sevs = [f.severity for f in report.findings]
    # critical → high → info
    assert sevs == sorted(sevs, key={"critical": 0, "high": 1, "medium": 2, "info": 3}.get)


def test_render_markdown_healthy_returns_one_line(cfg_db):
    cfg, db = cfg_db
    report = build_report(cfg, db)
    body = render_markdown(report)
    assert "All clear" in body


def test_render_markdown_includes_every_finding_with_cite(cfg_db):
    cfg, db = cfg_db
    db.upsert(_mk("foo", context={
        "cve": {"counts": {"critical": 1}, "top_critical": ["CVE-X"]},
    }))
    report = build_report(cfg, db)
    body = render_markdown(report)
    assert "CRITICAL" in body
    # Citation is mandatory — every finding line carries its source ref
    assert "counts.critical=1" in body


def test_write_to_notes_atomic(tmp_path):
    out = write_to_notes(tmp_path / "notes", "# audit body")
    assert out and out.exists()
    assert out.read_text().startswith("# audit body")


def test_write_to_notes_noop_when_empty():
    assert write_to_notes("", "x") is None


# ─── web routes ─────────────────────────────────────────────────────────


def test_audit_page_renders(tmp_path: Path):
    cfg = Config()
    cfg.web.auth.enabled = False
    cfg.scheduler.enabled = False
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    db = Database(cfg.storage.database_path)
    try:
        db.upsert(_mk("openclaw", context={
            "cve": {"counts": {"critical": 1}, "top_critical": ["CVE-2026-0001"]},
        }))
    finally:
        db.close()
    app = web.create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/audit")
        assert r.status_code == 200
        assert "Homelab audit" in r.text
        assert "CVE-2026-0001" in r.text


def test_audit_api_returns_json(tmp_path: Path):
    cfg = Config()
    cfg.web.auth.enabled = False
    cfg.scheduler.enabled = False
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    db = Database(cfg.storage.database_path)
    try:
        db.upsert(_mk("x", context={"orphan_since_days": 60}))
    finally:
        db.close()
    app = web.create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/api/audit")
        assert r.status_code == 200
        body = r.json()
        assert body["healthy"] is False
        assert any(f["category"] == "orphan" for f in body["findings"])


def test_run_audit_writes_to_notes_dir(tmp_path: Path):
    cfg = Config()
    cfg.scheduler.enabled = False
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    cfg.notes.notes_dir = str(tmp_path / "notes")
    db = Database(cfg.storage.database_path)
    try:
        db.upsert(_mk("x", context={"orphan_since_days": 90}))
        report, notes_path = run_audit(cfg, db)
    finally:
        db.close()
    assert notes_path == Path(cfg.notes.notes_dir) / "audit.md"
    assert "Homelab audit" in notes_path.read_text()
    assert report.healthy is False
