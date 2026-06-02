"""Tests for explain-mode (LLM prompt + raw response audit trail)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from homelabsage import web
from homelabsage.config import Config
from homelabsage.db import Database
from homelabsage.models import (
    Analysis,
    AnalyzedUpdate,
    Severity,
    Update,
    UpdateStatus,
)

# ─── DB layer ────────────────────────────────────────────────────────────


def test_upsert_explainer_overwrites_previous(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        db.upsert_explainer(
            "u1", prompt="p1", raw_response="r1",
            notes_used="n1", provider="ollama", model="m1",
        )
        db.upsert_explainer(
            "u1", prompt="p2", raw_response="r2",
            notes_used=None, provider="ollama", model="m2",
        )
        e = db.get_explainer("u1")
        assert e["prompt"] == "p2"
        assert e["raw_response"] == "r2"
        assert e["model"] == "m2"
        assert e["notes_used"] is None
    finally:
        db.close()


def test_get_explainer_returns_none_when_absent(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        assert db.get_explainer("nope") is None
    finally:
        db.close()


# ─── engine integration ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_engine_writes_explainer_after_successful_analyze(tmp_path: Path, monkeypatch):
    from homelabsage.engine import Engine

    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    cfg.llm.provider = "ollama"
    cfg.llm.endpoint = "http://stub"
    cfg.llm.model = "stub-model"

    class _FakePlugin:
        id = "fake"
        _emitted = False
        async def scan(self):
            if self._emitted:
                return []
            self._emitted = True
            return [Update(source="fake", subject="alpha", current_version="1", new_version="2")]

    async def fake_analyze(self, update, notes=""):
        from homelabsage.llm import LastCall
        self.last_call = LastCall(
            prompt="the prompt",
            raw_response='{"severity":"high","summary":"ok"}',
            tokens_in=10, tokens_out=5, tokens_estimated=False,
            duration_ms=42, succeeded=True,
        )
        return Analysis(severity=Severity.HIGH, summary="ok")

    monkeypatch.setattr("homelabsage.llm.LLMClient.analyze", fake_analyze)

    db = Database(cfg.storage.database_path)
    engine = Engine(cfg, db)
    engine.plugins = [_FakePlugin()]  # type: ignore[list-item]
    engine.outputs = []
    try:
        await engine.run_once()
        e = db.get_explainer("fake:alpha:2")
        assert e is not None
        assert e["prompt"] == "the prompt"
        assert e["raw_response"] == '{"severity":"high","summary":"ok"}'
        assert e["provider"] == "ollama"
        assert e["model"] == "stub-model"
    finally:
        engine.close()


# ─── web routes ──────────────────────────────────────────────────────────


def test_explain_page_renders_with_audit_trail(tmp_path: Path):
    cfg = Config()
    cfg.web.auth.enabled = False
    cfg.scheduler.enabled = False
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    db = Database(cfg.storage.database_path)
    try:
        item = AnalyzedUpdate(
            update=Update(source="docker", subject="x", current_version="1", new_version="2"),
            analysis=Analysis(severity=Severity.HIGH, summary="hi"),
            status=UpdateStatus.ANALYZED,
            detected_at=datetime(2026, 6, 1),
        )
        db.upsert(item)
        db.upsert_explainer(
            item.id,
            prompt="PROMPT BODY HERE",
            raw_response="RAW LLM RESPONSE",
            notes_used="user note text",
            provider="ollama",
            model="qwen3:30b",
        )
    finally:
        db.close()

    app = web.create_app(cfg)
    with TestClient(app) as c:
        r = c.get(f"/updates/{item.id}/explain")
        assert r.status_code == 200
        assert "Why this verdict?" in r.text
        assert "PROMPT BODY HERE" in r.text
        assert "RAW LLM RESPONSE" in r.text
        assert "user note text" in r.text


def test_explain_api_returns_404_when_no_explainer_stored(tmp_path: Path):
    cfg = Config()
    cfg.web.auth.enabled = False
    cfg.scheduler.enabled = False
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    db = Database(cfg.storage.database_path)
    try:
        item = AnalyzedUpdate(
            update=Update(source="docker", subject="y", current_version="1", new_version="2"),
            analysis=Analysis(severity=Severity.INFO, summary="—"),
            status=UpdateStatus.ANALYZED,
            detected_at=datetime(2026, 6, 1),
        )
        db.upsert(item)
    finally:
        db.close()

    app = web.create_app(cfg)
    with TestClient(app) as c:
        r = c.get(f"/api/updates/{item.id}/explain")
        assert r.status_code == 404


def test_explain_404_for_unknown_update(tmp_path: Path):
    cfg = Config()
    cfg.web.auth.enabled = False
    cfg.scheduler.enabled = False
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    app = web.create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/updates/nope/explain")
        assert r.status_code == 404
