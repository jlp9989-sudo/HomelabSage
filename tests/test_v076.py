"""Tests for v0.7.6: MCP compose_graph_mermaid + /api/updates/recurring-failures.

audit-prune (db + CLI), clear_all_snoozes, and doctor --severity-floor
tests moved to test_audit_history.py, test_snooze.py, and test_doctor.py
respectively in v0.11.6 / v0.11.7 / v0.11.8.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from homelabsage.config import Config
from homelabsage.db import Database


def _seed_update(db, *, source="x", subject="y"):
    from homelabsage.models import AnalyzedUpdate, Update
    item = AnalyzedUpdate(
        update=Update(
            source=source, subject=subject,
            current_version="1", new_version="2",
        ),
    )
    db.upsert(item)
    return item


# ─── MCP compose_graph_mermaid ────────────────────────────────────


def test_mcp_compose_graph_mermaid_with_real_compose(tmp_path):
    from homelabsage.mcp import TOOLS
    compose_dir = tmp_path / "stack"
    compose_dir.mkdir()
    (compose_dir / "docker-compose.yml").write_text(
        "services:\n"
        "  web:\n"
        "    image: nginx\n"
        "    depends_on: [db]\n"
        "  db:\n"
        "    image: postgres\n",
    )
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["compose_graph_mermaid"]["impl"]
    out = impl(cfg, db, {"paths": [str(compose_dir)]})
    assert out["service_count"] == 2
    assert out["edge_count"] == 1
    assert "web --> db" in out["mermaid"]


def test_mcp_compose_graph_mermaid_no_paths():
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["compose_graph_mermaid"]["impl"]
    out = impl(cfg, db, {})
    assert out["service_count"] == 0
    assert "no compose scan paths" in out["reason"]


# ─── /api/updates/recurring-failures ──────────────────────────────


def test_api_recurring_failures(tmp_path):
    from homelabsage.models import UpdateStatus
    from homelabsage.web import create_app
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    item = _seed_update(db, subject="flaky")
    # Need real transitions to bump count after v0.7.1 fix
    db.set_status(item.id, UpdateStatus.FAILED)
    db.set_status(item.id, UpdateStatus.APPLIED)
    db.set_status(item.id, UpdateStatus.FAILED)
    db.set_status(item.id, UpdateStatus.APPLIED)
    db.set_status(item.id, UpdateStatus.FAILED)
    client = TestClient(create_app(cfg))
    r = client.get("/api/updates/recurring-failures?min_count=2")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["items"][0]["failure_count"] == 3
