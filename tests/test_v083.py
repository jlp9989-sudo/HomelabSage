"""Tests for v0.8.3: /api/audit/categories + MCP is_snoozed."""

from __future__ import annotations

from fastapi.testclient import TestClient

from homelabsage.config import Config


def _seed(db, *, subject="y"):
    from homelabsage.models import AnalyzedUpdate, Update
    item = AnalyzedUpdate(
        update=Update(
            source="x", subject=subject,
            current_version="1", new_version="2",
        ),
    )
    db.upsert(item)
    return item


# ─── /api/audit/categories ────────────────────────────────────────


def test_api_audit_categories_shape(tmp_path):
    from homelabsage.web import create_app
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    client = TestClient(create_app(cfg))
    r = client.get("/api/audit/categories")
    assert r.status_code == 200
    body = r.json()
    assert "counts_by_category" in body
    assert "counts_by_severity" in body
    assert body["total"] == 0
    assert body["healthy"] is True
    # Defensive: no findings list (the whole point)
    assert "findings" not in body


# MCP is_snoozed tests moved to test_snooze.py in v0.11.6.
