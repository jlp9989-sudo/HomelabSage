"""Tests for v0.8.3: /api/audit/categories + MCP is_snoozed."""

from __future__ import annotations

from fastapi.testclient import TestClient

from homelabsage.config import Config
from homelabsage.db import Database


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


# ─── MCP is_snoozed ────────────────────────────────────────────────


def test_mcp_is_snoozed_false_when_no_snooze(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(str(tmp_path / "t.db"))
    item = _seed(db)
    impl = TOOLS["is_snoozed"]["impl"]
    out = impl(cfg, db, {"update_id": item.id})
    assert out["snoozed"] is False
    assert out["snooze_until"] is None


def test_mcp_is_snoozed_true_for_future(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(str(tmp_path / "t.db"))
    item = _seed(db)
    db.set_snooze(item.id, "2199-01-01T00:00:00+00:00")
    impl = TOOLS["is_snoozed"]["impl"]
    out = impl(cfg, db, {"update_id": item.id})
    assert out["snoozed"] is True
    assert out["snooze_until"] == "2199-01-01T00:00:00+00:00"


def test_mcp_is_snoozed_false_for_past(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(str(tmp_path / "t.db"))
    item = _seed(db)
    db.set_snooze(item.id, "2020-01-01T00:00:00+00:00")
    impl = TOOLS["is_snoozed"]["impl"]
    out = impl(cfg, db, {"update_id": item.id})
    assert out["snoozed"] is False
    assert out["snooze_until"] == "2020-01-01T00:00:00+00:00"


def test_mcp_is_snoozed_handles_corrupt(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(str(tmp_path / "t.db"))
    item = _seed(db)
    db.set_snooze(item.id, "garbage-not-iso")
    impl = TOOLS["is_snoozed"]["impl"]
    out = impl(cfg, db, {"update_id": item.id})
    assert out["snoozed"] is False
    assert "error" in out


def test_mcp_is_snoozed_requires_update_id():
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["is_snoozed"]["impl"]
    out = impl(cfg, db, {})
    assert out["snoozed"] is False
    assert "error" in out
