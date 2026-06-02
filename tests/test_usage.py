"""Tests for LLM-usage tracking + the /usage page."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from homelabsage import web
from homelabsage.config import Config
from homelabsage.db import Database
from homelabsage.db.usage import estimate_tokens


def test_estimate_tokens_chars_div_four():
    assert estimate_tokens("") == 1     # min floor
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 400) == 100


def test_record_llm_call_writes_row(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        rid = db.record_llm_call(
            provider="ollama", model="qwen3:30b",
            update_id="docker:x:2",
            tokens_in=400, tokens_out=100,
            estimated=False, duration_ms=8800,
            succeeded=True,
        )
        assert rid > 0
        recent = db.recent_llm_calls(limit=5)
        assert len(recent) == 1
        assert recent[0]["tokens_in"] == 400
        assert recent[0]["succeeded"] == 1
    finally:
        db.close()


def test_usage_summary_aggregates_by_provider_model(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        for _ in range(3):
            db.record_llm_call(
                provider="groq", model="llama-3.3-70b-versatile",
                update_id=None, tokens_in=100, tokens_out=50,
                estimated=False, duration_ms=1000, succeeded=True,
            )
        db.record_llm_call(
            provider="ollama", model="qwen3:30b",
            update_id=None, tokens_in=400, tokens_out=80,
            estimated=True, duration_ms=8000, succeeded=True,
        )
        # 1 failed call to verify succeeded counter
        db.record_llm_call(
            provider="groq", model="llama-3.3-70b-versatile",
            update_id=None, tokens_in=50, tokens_out=0,
            estimated=True, duration_ms=500, succeeded=False,
        )
        s = db.usage_summary()
        assert s["totals"]["calls"] == 5
        assert s["totals"]["tokens_in"] == 100*3 + 400 + 50
        groq_row = next(r for r in s["by_provider_model"] if r["provider"] == "groq")
        assert groq_row["calls"] == 4
        assert groq_row["succeeded"] == 3
    finally:
        db.close()


def test_usage_page_renders(tmp_path: Path):
    cfg = Config()
    cfg.web.auth.enabled = False
    cfg.scheduler.enabled = False
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    db = Database(cfg.storage.database_path)
    try:
        db.record_llm_call(
            provider="ollama", model="qwen3:30b",
            update_id="docker:x:2", tokens_in=400, tokens_out=100,
            estimated=False, duration_ms=8800, succeeded=True,
        )
    finally:
        db.close()
    app = web.create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/usage")
        assert r.status_code == 200
        assert "LLM usage" in r.text
        assert "qwen3:30b" in r.text


def test_usage_api_returns_summary_and_recent(tmp_path: Path):
    cfg = Config()
    cfg.web.auth.enabled = False
    cfg.scheduler.enabled = False
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    db = Database(cfg.storage.database_path)
    try:
        db.record_llm_call(
            provider="ollama", model="m", update_id=None,
            tokens_in=10, tokens_out=5, estimated=False,
            duration_ms=100, succeeded=True,
        )
    finally:
        db.close()
    app = web.create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/api/usage")
        assert r.status_code == 200
        body = r.json()
        assert body["summary"]["totals"]["calls"] == 1
        assert len(body["recent"]) == 1
