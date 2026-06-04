"""Tests for v0.7.1: doctor module + /api/doctor + MCP doctor tool."""

from __future__ import annotations

from fastapi.testclient import TestClient

from homelabsage.config import Config
from homelabsage.db import Database
from homelabsage.doctor import build_report


def test_doctor_report_skips_when_unconfigured(tmp_path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    report = build_report(cfg, db, skip_llm=True)
    # Most sections are unconfigured → all `skipped: True`
    sections = report["sections"]
    assert sections["llm"]["skipped"] is True
    assert sections["tls"]["skipped"] is True
    assert sections["dns"]["skipped"] is True
    assert sections["disk"]["skipped"] is True
    assert sections["compose"]["skipped"] is True
    # Audit always runs (always available)
    assert "audit" in sections
    # Overall healthy because nothing complained
    assert report["healthy"] is True


def test_doctor_report_skip_llm_flag(tmp_path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    report = build_report(cfg, db, skip_llm=True)
    assert report["sections"]["llm"]["skipped"]
    assert report["llm_unreachable"] is False


def test_doctor_report_llm_unreachable_flag(tmp_path, monkeypatch):
    cfg = Config()
    cfg.llm.endpoint = "http://127.0.0.1:1"   # nothing listens on port 1
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    report = build_report(cfg, db, skip_llm=False)
    assert report["llm_unreachable"] is True
    assert report["healthy"] is False


def test_api_doctor_returns_json(tmp_path):
    from homelabsage.web import create_app
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    app = create_app(cfg)
    client = TestClient(app)
    r = client.get("/api/doctor?skip_llm=1")
    assert r.status_code == 200
    body = r.json()
    assert "healthy" in body
    assert "sections" in body
    assert body["sections"]["llm"]["skipped"]


def test_snooze_blocks_flush_pending_dispatches(tmp_path):
    """v0.7.1 review fix: queued pushes must skip when snoozed."""
    import asyncio

    from homelabsage.engine import Engine
    from homelabsage.models import (
        Analysis,
        AnalyzedUpdate,
        Severity,
        Update,
    )
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    item = AnalyzedUpdate(
        update=Update(
            source="x", subject="y",
            current_version="1", new_version="2",
        ),
        analysis=Analysis(severity=Severity.HIGH, summary="m"),
    )
    db.upsert(item)

    # Build a stub push output that records calls.
    calls: list[str] = []

    class _StubPush:
        id = "stub"
        is_push = True

        async def send(self, it):
            calls.append(it.id)

    engine = Engine(cfg, db)
    engine.outputs = [_StubPush()]
    try:
        # Queue + snooze into the future
        db.queue_pending_dispatch(item.id, "stub")
        db.set_snooze(item.id, "2199-12-31T00:00:00+00:00")
        asyncio.run(engine._flush_pending_dispatches())
        # snooze in flush MUST suppress the call
        assert calls == []
        # Queue row stays so it retries when snooze expires
        pending = db.list_pending_dispatches()
        assert len(pending) == 1
    finally:
        engine.close()


def test_mcp_doctor_tool(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    impl = TOOLS["doctor"]["impl"]
    out = impl(cfg, db, {"skip_llm": True})
    assert "healthy" in out
    assert "sections" in out
    assert out["sections"]["llm"]["skipped"]
