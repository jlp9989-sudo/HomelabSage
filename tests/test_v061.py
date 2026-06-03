"""Tests for v0.6.1: search endpoint, /api/inbox receiver, MCP tools."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from homelabsage import web
from homelabsage.config import Config, load_config
from homelabsage.db import Database
from homelabsage.mcp import TOOLS, dispatch
from homelabsage.models import (
    Analysis,
    AnalyzedUpdate,
    Severity,
    Update,
    UpdateStatus,
)


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
    return web.create_app(cfg, cfg_path=cfg_path), cfg


def _db_from_app(app):
    return next(
        r for r in app.router.routes if hasattr(r, "endpoint")
        and r.endpoint.__name__ == "set_status"
    ).endpoint.__closure__[0].cell_contents


def _seed(db, items: list[tuple[str, str]]):
    """`items` is list of (subject, summary). All severity=high."""
    for subject, summary in items:
        db.upsert(AnalyzedUpdate(
            update=Update(source="docker", subject=subject,
                          current_version="1", new_version="2"),
            analysis=Analysis(severity=Severity.HIGH, summary=summary,
                              breaking_changes=[]),
            status=UpdateStatus.ANALYZED,
        ))


# ─── DB.search ──────────────────────────────────────────────────────


def test_db_search_matches_subject(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        _seed(db, [("mealie", "recipe app"), ("kavita", "book reader")])
        out = db.search("mealie")
        assert len(out) == 1
        assert out[0].update.subject == "mealie"
    finally:
        db.close()


def test_db_search_matches_summary(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        _seed(db, [("a", "data dir moved"), ("b", "no changes")])
        out = db.search("data dir")
        assert len(out) == 1
        assert out[0].update.subject == "a"
    finally:
        db.close()


def test_db_search_case_insensitive(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        _seed(db, [("Mealie", "x")])
        assert len(db.search("MEALIE")) == 1
        assert len(db.search("mealie")) == 1
    finally:
        db.close()


def test_db_search_empty_returns_empty(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        _seed(db, [("x", "y")])
        assert db.search("") == []
        assert db.search("   ") == []
    finally:
        db.close()


def test_db_search_matches_user_note(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        _seed(db, [("a", "x"), ("b", "y")])
        a_id = "docker:a:2"
        db.set_user_note(a_id, "look at this carefully")
        out = db.search("carefully")
        assert len(out) == 1
        assert out[0].update.subject == "a"
    finally:
        db.close()


# ─── /api/updates/search ────────────────────────────────────────────


def test_api_search_returns_envelope(tmp_path: Path):
    app, _ = _bare_app(tmp_path)
    db = _db_from_app(app)
    _seed(db, [("mealie", "x"), ("kavita", "y")])
    with TestClient(app) as client:
        r = client.get("/api/updates/search", params={"q": "mealie"})
        assert r.status_code == 200
        body = r.json()
        assert body["query"] == "mealie"
        assert body["count"] == 1


def test_api_search_caps_limit(tmp_path: Path):
    app, _ = _bare_app(tmp_path)
    with TestClient(app) as client:
        # Out-of-range limit gets clamped, not rejected.
        r = client.get(
            "/api/updates/search",
            params={"q": "x", "limit": "9999"},
        )
        assert r.status_code == 200


# ─── /api/inbox/<source> ────────────────────────────────────────────


def test_inbox_creates_update(tmp_path: Path):
    app, _ = _bare_app(tmp_path)
    db = _db_from_app(app)
    with TestClient(app) as client:
        r = client.post(
            "/api/inbox/external",
            json={
                "subject": "my-thing",
                "current_version": "1.0",
                "new_version": "2.0",
                "release_url": "https://example/release",
            },
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # Read while the DB connection is still open.
        item = db.get("external:my-thing:2.0")
        assert item is not None
        assert item.update.source == "external"
        assert item.update.subject == "my-thing"


def test_inbox_rejects_bad_source(tmp_path: Path):
    app, _ = _bare_app(tmp_path)
    with TestClient(app) as client:
        r = client.post(
            "/api/inbox/UPPER%20case",
            json={"subject": "x", "new_version": "2"},
        )
        assert r.status_code in (400, 404)


def test_inbox_requires_subject_and_version(tmp_path: Path):
    app, _ = _bare_app(tmp_path)
    with TestClient(app) as client:
        r = client.post(
            "/api/inbox/ok",
            json={"new_version": "2"},  # missing subject
        )
        assert r.status_code == 400
        r = client.post(
            "/api/inbox/ok",
            json={"subject": "x"},  # missing new_version
        )
        assert r.status_code == 400


def test_inbox_rejects_non_object_context(tmp_path: Path):
    app, _ = _bare_app(tmp_path)
    with TestClient(app) as client:
        r = client.post(
            "/api/inbox/ok",
            json={
                "subject": "x", "new_version": "2",
                "context": "not an object",
            },
        )
        assert r.status_code == 400


def test_inbox_accepts_context_dict(tmp_path: Path):
    app, _ = _bare_app(tmp_path)
    db = _db_from_app(app)
    with TestClient(app) as client:
        r = client.post(
            "/api/inbox/ok",
            json={
                "subject": "x", "new_version": "2",
                "context": {"image": "x:2", "extra": "k"},
            },
        )
        assert r.status_code == 200
        # Read the row WHILE the TestClient is alive — lifecycle's
        # shutdown handler closes the DB connection on exit.
        item = db.get("ok:x:2")
        assert item is not None
        assert item.update.context["image"] == "x:2"


# ─── MCP tools ──────────────────────────────────────────────────────


def test_mcp_registers_new_tools():
    for name in ("get_user_note", "set_user_note", "search_updates"):
        assert name in TOOLS


def test_mcp_search_updates_requires_query(tmp_path: Path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    db = Database(cfg.storage.database_path)
    try:
        r = dispatch(cfg, db, {
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {"name": "search_updates", "arguments": {}},
        })
        assert r["error"]["code"] == -32602
    finally:
        db.close()


def test_mcp_search_updates_returns_hits(tmp_path: Path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    db = Database(cfg.storage.database_path)
    try:
        _seed(db, [("mealie", "x"), ("kavita", "y")])
        r = dispatch(cfg, db, {
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {"name": "search_updates",
                       "arguments": {"q": "kav"}},
        })
        data = r["result"]["structuredContent"]
        assert data["count"] == 1
        assert data["items"][0]["subject"] == "kavita"
    finally:
        db.close()


def test_mcp_user_note_round_trip(tmp_path: Path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    db = Database(cfg.storage.database_path)
    try:
        _seed(db, [("x", "x")])
        # Set
        r = dispatch(cfg, db, {
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {"name": "set_user_note",
                       "arguments": {"update_id": "docker:x:2",
                                     "note": "investigate"}},
        })
        assert r["result"]["structuredContent"]["note"] == "investigate"
        # Get
        r = dispatch(cfg, db, {
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {"name": "get_user_note",
                       "arguments": {"update_id": "docker:x:2"}},
        })
        assert r["result"]["structuredContent"]["note"] == "investigate"
    finally:
        db.close()


def test_mcp_user_note_unknown_id(tmp_path: Path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    db = Database(cfg.storage.database_path)
    try:
        r = dispatch(cfg, db, {
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {"name": "set_user_note",
                       "arguments": {"update_id": "nope", "note": "x"}},
        })
        assert r["error"]["code"] == -32602
    finally:
        db.close()
