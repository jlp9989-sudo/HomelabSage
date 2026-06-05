"""Tests for v0.9.1: /api/audit/by-category + MCP system_info."""

from __future__ import annotations

from fastapi.testclient import TestClient

from homelabsage import __version__
from homelabsage.config import Config
from homelabsage.db import Database


def _seed_abandoned(db, *, subject):
    """Seed an update whose context surfaces as an `abandoned` finding."""
    from homelabsage.models import (
        Analysis,
        AnalyzedUpdate,
        Severity,
        Update,
    )
    item = AnalyzedUpdate(
        update=Update(
            source="docker", subject=subject,
            current_version="1", new_version="2",
            context={
                "repo_health": {
                    "status": "abandoned", "reason": "no push 18mo",
                },
            },
        ),
        analysis=Analysis(severity=Severity.HIGH, summary="m"),
    )
    db.upsert(item)
    return item


# ─── /api/audit/by-category ───────────────────────────────────────


def test_api_audit_by_category_returns_only_matching(tmp_path):
    from homelabsage.web import create_app
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    _seed_abandoned(db, subject="legacy-thing")
    client = TestClient(create_app(cfg))
    r = client.get("/api/audit/by-category?category=abandoned")
    assert r.status_code == 200
    body = r.json()
    assert body["category"] == "abandoned"
    assert body["count"] == 1
    assert body["items"][0]["category"] == "abandoned"


def test_api_audit_by_category_empty_string_returns_empty(tmp_path):
    from homelabsage.web import create_app
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    client = TestClient(create_app(cfg))
    r = client.get("/api/audit/by-category?category=")
    assert r.status_code == 200
    assert r.json() == {"count": 0, "items": [], "category": ""}


def test_api_audit_by_category_unknown_returns_empty(tmp_path):
    from homelabsage.web import create_app
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    _seed_abandoned(db, subject="x")
    client = TestClient(create_app(cfg))
    r = client.get("/api/audit/by-category?category=no-such-category")
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_api_audit_by_category_clamps_limit(tmp_path):
    from homelabsage.web import create_app
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    client = TestClient(create_app(cfg))
    r = client.get("/api/audit/by-category?category=x&limit=999999")
    assert r.status_code == 200


# ─── MCP system_info ─────────────────────────────────────────────


def test_mcp_system_info_empty_db(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    impl = TOOLS["system_info"]["impl"]
    out = impl(cfg, db, {})
    assert out["version"] == __version__
    assert out["updates_by_status"]["new"] == 0
    assert out["audit_total"] == 0
    assert out["snoozed_total"] == 0
    assert out["audit_mutes_total"] == 0
    assert out["pending_dispatches"] == 0


def test_mcp_system_info_counts_state(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    item = _seed_abandoned(db, subject="x")
    db.set_snooze(item.id, "2199-01-01T00:00:00+00:00")
    db.add_audit_mute(category="a", source_kind="b", source_ref="c")
    db.queue_pending_dispatch(item.id, "notion")
    impl = TOOLS["system_info"]["impl"]
    out = impl(cfg, db, {})
    assert out["updates_by_status"]["new"] == 1
    assert out["snoozed_total"] == 1
    assert out["audit_mutes_total"] == 1
    assert out["pending_dispatches"] == 1
    assert out["audit_total"] >= 1
