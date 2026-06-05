"""Tests for v0.7.8: status CLI + MCP clear_pending/version + /api/version."""

from __future__ import annotations

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from homelabsage import __version__
from homelabsage.cli import app
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


# ─── homelabsage status ────────────────────────────────────────────


def test_status_cli_prints_summary(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    db_path = tmp_path / "h.db"
    cfg_path.write_text(
        f"""storage:
  database_path: {db_path}
llm:
  endpoint: ""
""",
    )
    # Seed some state so the summary has shape
    db = Database(str(db_path))
    _seed(db, subject="hello")
    db.close()
    runner = CliRunner()
    result = runner.invoke(app, ["status", "--config", str(cfg_path)])
    assert result.exit_code == 0
    assert "HomelabSage" in result.stdout
    assert "Updates" in result.stdout
    assert "Audit" in result.stdout
    assert "Parity gate" in result.stdout
    assert "Pending dispatches" in result.stdout


def test_status_cli_includes_version(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f"""storage:
  database_path: {tmp_path / 'h.db'}
llm:
  endpoint: ""
""",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["status", "--config", str(cfg_path)])
    assert __version__ in result.stdout


# ─── MCP clear_pending_dispatches ──────────────────────────────────


def test_db_clear_pending_dispatches(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    a = _seed(db, subject="a")
    b = _seed(db, subject="b")
    db.queue_pending_dispatch(a.id, "out1")
    db.queue_pending_dispatch(b.id, "out2")
    assert db.clear_pending_dispatches() == 2
    # Idempotent: second call clears 0
    assert db.clear_pending_dispatches() == 0
    assert db.list_pending_dispatches() == []


def test_mcp_clear_pending_dispatches(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(str(tmp_path / "t.db"))
    item = _seed(db)
    db.queue_pending_dispatch(item.id, "out1")
    impl = TOOLS["clear_pending_dispatches"]["impl"]
    out = impl(cfg, db, {})
    assert out["ok"] is True
    assert out["cleared"] == 1


# ─── MCP version + /api/version ───────────────────────────────────


def test_mcp_version_returns_version():
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["version"]["impl"]
    out = impl(cfg, db, {})
    assert out["version"] == __version__
    assert isinstance(out["features"], dict)
    assert out["features"]["doctor"] is True


def test_api_version(tmp_path):
    from homelabsage.web import create_app
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    client = TestClient(create_app(cfg))
    r = client.get("/api/version")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == __version__
    assert body["features"]["snooze"] is True


def test_api_version_is_auth_bypassed():
    """`/api/version` is in AUTH_BYPASS_EXACT."""
    from homelabsage.web.auth import AUTH_BYPASS_EXACT
    assert "/api/version" in AUTH_BYPASS_EXACT
