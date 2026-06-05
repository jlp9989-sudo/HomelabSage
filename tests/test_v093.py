"""Tests for v0.9.3: dormant detector wiring + MCP get_chronicle."""

from __future__ import annotations

from homelabsage.audit import build_report
from homelabsage.config import Config
from homelabsage.db import Database
from homelabsage.models import (
    Analysis,
    AnalyzedUpdate,
    Severity,
    Update,
    UpdateStatus,
)


def _seed_with_context(db, *, subject, context):
    item = AnalyzedUpdate(
        update=Update(
            source="docker", subject=subject,
            current_version="1", new_version="2",
            context=context,
        ),
        analysis=Analysis(severity=Severity.HIGH, summary="m"),
        status=UpdateStatus.NEW,
    )
    db.upsert(item)
    return item


# ─── restart_freq → audit finding ─────────────────────────────────


def test_restart_freq_context_surfaces_as_finding(tmp_path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    _seed_with_context(db, subject="flappy", context={
        "restart_freq": {
            "severity": "high",
            "restart_count": 42,
            "restarts_per_hour": 5.3,
        },
    })
    report = build_report(cfg, db)
    flap = [f for f in report.findings if f.category == "restart_flapping"]
    assert len(flap) == 1
    assert flap[0].severity == "high"
    assert "5.30" in flap[0].cite or "5.3" in flap[0].cite


def test_restart_freq_info_severity_ignored(tmp_path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    _seed_with_context(db, subject="calm", context={
        "restart_freq": {"severity": "info", "restart_count": 1,
                         "restarts_per_hour": 0.01},
    })
    report = build_report(cfg, db)
    assert not any(
        f.category == "restart_flapping" for f in report.findings
    )


# ─── healthcheck_stale → audit finding ───────────────────────────


def test_healthcheck_stale_context_surfaces(tmp_path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    _seed_with_context(db, subject="unhealthy", context={
        "healthcheck_stale": {
            "severity": "high",
            "failing_streak": 12,
            "hours_unhealthy": 48.5,
        },
    })
    report = build_report(cfg, db)
    hs = [f for f in report.findings if f.category == "healthcheck_stale"]
    assert len(hs) == 1
    assert hs[0].severity == "high"
    assert "48.5h" in hs[0].title


def test_healthcheck_stale_handles_null_hours(tmp_path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    _seed_with_context(db, subject="x", context={
        "healthcheck_stale": {
            "severity": "medium",
            "failing_streak": 3,
            "hours_unhealthy": None,
        },
    })
    report = build_report(cfg, db)
    hs = [f for f in report.findings if f.category == "healthcheck_stale"]
    assert len(hs) == 1
    assert "?" in hs[0].title


# ─── exposed_ports → audit finding ───────────────────────────────


def test_exposed_ports_picks_worst_severity(tmp_path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    _seed_with_context(db, subject="leaky", context={
        "exposed_ports": [
            {"severity": "medium", "port": "8080/tcp"},
            {"severity": "high", "port": "22/tcp"},
            {"severity": "info", "port": "9000/tcp"},
        ],
    })
    report = build_report(cfg, db)
    ep = [f for f in report.findings if f.category == "exposed_ports"]
    assert len(ep) == 1
    assert ep[0].severity == "high"
    # 3 ports total in the title
    assert "3 risky" in ep[0].title


def test_exposed_ports_skipped_when_only_info(tmp_path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    _seed_with_context(db, subject="x", context={
        "exposed_ports": [{"severity": "info", "port": "80/tcp"}],
    })
    report = build_report(cfg, db)
    assert not any(f.category == "exposed_ports" for f in report.findings)


# ─── MCP get_chronicle ────────────────────────────────────────────


def test_mcp_get_chronicle_returns_timeline(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    # Seed an applied item — will surface as a kind="applied" entry
    item = _seed_with_context(db, subject="mealie", context={})
    db.set_status(item.id, UpdateStatus.APPLIED)
    impl = TOOLS["get_chronicle"]["impl"]
    out = impl(cfg, db, {"days": 30})
    assert "entries" in out
    assert isinstance(out["counts_by_kind"], dict)
    assert any(
        e["kind"] == "applied" and e["subject"] == "mealie"
        for e in out["entries"]
    )


def test_mcp_get_chronicle_clamps_days(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    impl = TOOLS["get_chronicle"]["impl"]
    # Out-of-range days should not crash; the helper clamps 1..365.
    out = impl(cfg, db, {"days": 999999})
    assert "period_start" in out
    out = impl(cfg, db, {"days": 0})
    assert "period_start" in out
