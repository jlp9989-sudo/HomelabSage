"""Tests for v0.6.3: star/bookmark + Pushover output + MCP explain/star
+ Notion archive CLI."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from homelabsage import web
from homelabsage.config import Config, PushoverOutputConfig, load_config
from homelabsage.db import Database
from homelabsage.mcp import TOOLS, dispatch
from homelabsage.models import (
    Analysis,
    AnalyzedUpdate,
    Severity,
    Update,
    UpdateStatus,
)
from homelabsage.outputs.pushover import PushoverOutput


def _bare_app(tmp_path: Path):
    body = (
        "llm:\n  provider: disabled\n  model: stub\n  endpoint: http://stub\n"
        "scheduler:\n  enabled: false\n"
        f"storage:\n  database_path: {tmp_path}/state.sqlite\n"
        "web:\n  enabled: true\n  host: 127.0.0.1\n  port: 0\n"
        "  auth:\n    enabled: false\n"
    )
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(body)
    cfg = load_config(cfg_path)
    return web.create_app(cfg, cfg_path=cfg_path)


def _db_from_app(app):
    return next(
        r for r in app.router.routes if hasattr(r, "endpoint")
        and r.endpoint.__name__ == "set_status"
    ).endpoint.__closure__[0].cell_contents


# ─── star/bookmark ──────────────────────────────────────────────────


def _seed(db, subject: str = "x") -> str:
    item = AnalyzedUpdate(
        update=Update(source="docker", subject=subject,
                      current_version="1", new_version="2"),
        analysis=Analysis(severity=Severity.HIGH, summary="m"),
        status=UpdateStatus.ANALYZED,
    )
    db.upsert(item)
    return item.id


def test_star_db_round_trip(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        uid = _seed(db)
        assert db.is_starred(uid) is False
        assert db.set_starred(uid, True) is True
        assert db.is_starred(uid) is True
        assert db.set_starred(uid, False) is True
        assert db.is_starred(uid) is False
    finally:
        db.close()


def test_star_unknown_id_returns_false(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        assert db.set_starred("nope:x:1", True) is False
        assert db.is_starred("nope:x:1") is False
    finally:
        db.close()


def test_list_starred_returns_only_flagged(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        a = _seed(db, "a")
        _seed(db, "b")
        db.set_starred(a, True)
        starred = db.list_starred()
        assert len(starred) == 1
        assert starred[0].update.subject == "a"
    finally:
        db.close()


def test_star_api_round_trip(tmp_path: Path):
    app = _bare_app(tmp_path)
    db = _db_from_app(app)
    uid = _seed(db)
    with TestClient(app) as client:
        # Set starred
        r = client.post(f"/api/updates/{uid}/star", json={"starred": True})
        assert r.status_code == 200
        assert r.json()["starred"] is True
        # GET list
        r = client.get("/api/updates/starred")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        # Un-star
        r = client.post(f"/api/updates/{uid}/star", json={"starred": False})
        assert r.json()["starred"] is False


def test_star_api_404(tmp_path: Path):
    app = _bare_app(tmp_path)
    with TestClient(app) as client:
        r = client.post("/api/updates/nope:x:1/star",
                        json={"starred": True})
        assert r.status_code == 404


def test_star_api_rejects_non_bool(tmp_path: Path):
    app = _bare_app(tmp_path)
    db = _db_from_app(app)
    uid = _seed(db)
    with TestClient(app) as client:
        r = client.post(f"/api/updates/{uid}/star",
                        json={"starred": "yes please"})
        assert r.status_code == 400


# ─── Pushover output ────────────────────────────────────────────────


def test_pushover_skips_when_disabled():
    cfg = PushoverOutputConfig(enabled=False)
    out = PushoverOutput(cfg)
    item = AnalyzedUpdate(
        update=Update(source="x", subject="y",
                      current_version="1", new_version="2"),
        analysis=Analysis(severity=Severity.HIGH, summary="m"),
    )
    asyncio.run(out.send(item))


def test_pushover_skips_when_missing_tokens():
    """No token / user_key → don't send."""
    cfg = PushoverOutputConfig(enabled=True, token="", user_key="")
    out = PushoverOutput(cfg)
    item = AnalyzedUpdate(
        update=Update(source="x", subject="y",
                      current_version="1", new_version="2"),
        analysis=Analysis(severity=Severity.HIGH, summary="m"),
    )
    assert not out._should_send(item)


def test_pushover_form_carries_severity_priority():
    cfg = PushoverOutputConfig(
        enabled=True, token="apptoken", user_key="userkey",
    )
    out = PushoverOutput(cfg)
    crit = AnalyzedUpdate(
        update=Update(source="docker", subject="mealie",
                      current_version="1", new_version="2",
                      release_url="https://example/r"),
        analysis=Analysis(severity=Severity.CRITICAL, summary="m",
                          breaking_changes=["B1"]),
    )
    form = out._build_form(crit)
    assert form["token"] == "apptoken"
    assert form["user"] == "userkey"
    assert form["priority"] == "1"  # default — not emergency
    assert "B1" in form["message"]
    assert form["url"] == "https://example/r"


def test_pushover_emergency_opt_in():
    cfg = PushoverOutputConfig(
        enabled=True, token="t", user_key="u",
        emergency_at_critical=True,
    )
    out = PushoverOutput(cfg)
    crit = AnalyzedUpdate(
        update=Update(source="x", subject="y",
                      current_version="1", new_version="2"),
        analysis=Analysis(severity=Severity.CRITICAL, summary="m"),
    )
    form = out._build_form(crit)
    assert form["priority"] == "2"
    # Emergency requires retry/expire — Pushover rejects priority=2 without
    assert form["retry"] == "60"
    assert form["expire"] == "3600"


def test_pushover_truncates_long_message():
    cfg = PushoverOutputConfig(enabled=True, token="t", user_key="u")
    out = PushoverOutput(cfg)
    long_summary = "x" * 4000
    item = AnalyzedUpdate(
        update=Update(source="x", subject="y",
                      current_version="1", new_version="2"),
        analysis=Analysis(severity=Severity.HIGH, summary=long_summary),
    )
    form = out._build_form(item)
    assert len(form["message"]) == 1024


def test_pushover_respects_min_severity():
    cfg = PushoverOutputConfig(
        enabled=True, token="t", user_key="u", min_severity="high",
    )
    out = PushoverOutput(cfg)
    low = AnalyzedUpdate(
        update=Update(source="x", subject="y",
                      current_version="1", new_version="2"),
        analysis=Analysis(severity=Severity.MEDIUM, summary="m"),
    )
    assert not out._should_send(low)


# ─── MCP tools ──────────────────────────────────────────────────────


def test_mcp_registers_v063_tools():
    for name in ("explain", "set_star", "list_starred"):
        assert name in TOOLS


def test_mcp_set_star_requires_bool(tmp_path: Path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    db = Database(cfg.storage.database_path)
    try:
        uid = _seed(db)
        r = dispatch(cfg, db, {
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {"name": "set_star",
                       "arguments": {"update_id": uid, "starred": "yes"}},
        })
        assert r["error"]["code"] == -32602
    finally:
        db.close()


def test_mcp_set_star_round_trip(tmp_path: Path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    db = Database(cfg.storage.database_path)
    try:
        uid = _seed(db)
        r = dispatch(cfg, db, {
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {"name": "set_star",
                       "arguments": {"update_id": uid, "starred": True}},
        })
        assert r["result"]["structuredContent"]["starred"] is True
        # list_starred picks it up
        r = dispatch(cfg, db, {
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {"name": "list_starred", "arguments": {}},
        })
        data = r["result"]["structuredContent"]
        assert data["count"] == 1
        assert data["items"][0]["id"] == uid
    finally:
        db.close()


def test_mcp_explain_unknown_id(tmp_path: Path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    db = Database(cfg.storage.database_path)
    try:
        r = dispatch(cfg, db, {
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {"name": "explain",
                       "arguments": {"update_id": "nope"}},
        })
        assert r["error"]["code"] == -32602
        assert "no explainer" in r["error"]["message"]
    finally:
        db.close()


def test_mcp_explain_round_trip(tmp_path: Path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    db = Database(cfg.storage.database_path)
    try:
        uid = _seed(db)
        db.upsert_explainer(
            uid, prompt="hello", raw_response="ok",
            notes_used="(none)", provider="ollama", model="qwen",
        )
        r = dispatch(cfg, db, {
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {"name": "explain",
                       "arguments": {"update_id": uid}},
        })
        data = r["result"]["structuredContent"]
        assert data["prompt"] == "hello"
        assert data["raw_response"] == "ok"
    finally:
        db.close()
