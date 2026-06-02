"""Tests for the MCP JSON-RPC endpoint."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from homelabsage import web
from homelabsage.config import Config
from homelabsage.db import Database
from homelabsage.mcp import TOOLS, dispatch
from homelabsage.models import (
    Analysis,
    AnalyzedUpdate,
    Severity,
    Update,
    UpdateStatus,
)

# ─── unit tests against dispatch() (pure, no HTTP) ───────────────────────


@pytest.fixture
def cfg_db(tmp_path: Path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    cfg.sources.docker.enabled = True
    cfg.outputs.telegram.enabled = True
    cfg.outputs.telegram.bot_token = "x"
    cfg.outputs.telegram.chat_id = "y"
    db = Database(cfg.storage.database_path)
    yield cfg, db
    db.close()


def test_dispatch_rejects_non_jsonrpc_envelope(cfg_db):
    cfg, db = cfg_db
    resp = dispatch(cfg, db, {"method": "tools/list", "id": 1})
    assert "error" in resp
    assert resp["error"]["code"] == -32600


def test_dispatch_tools_list_returns_full_registry(cfg_db):
    cfg, db = cfg_db
    resp = dispatch(cfg, db, {"jsonrpc": "2.0", "method": "tools/list", "id": 1})
    assert "result" in resp
    names = {t["name"] for t in resp["result"]["tools"]}
    assert names == set(TOOLS.keys())
    # Every tool has a description + inputSchema
    for t in resp["result"]["tools"]:
        assert t["description"]
        assert t["inputSchema"]["type"] == "object"


def test_dispatch_unknown_method_returns_minus_32601(cfg_db):
    cfg, db = cfg_db
    resp = dispatch(cfg, db, {"jsonrpc": "2.0", "method": "tools/bogus", "id": 1})
    assert resp["error"]["code"] == -32601


def test_dispatch_health_lists_enabled_surfaces(cfg_db):
    cfg, db = cfg_db
    resp = dispatch(cfg, db, {
        "jsonrpc": "2.0", "method": "tools/call", "id": 1,
        "params": {"name": "health"},
    })
    data = resp["result"]["structuredContent"]
    assert "docker" in data["enabled_sources"]
    assert "telegram" in data["enabled_outputs"]
    assert data["parity_gate_enabled"] is False


def test_dispatch_list_updates_returns_count(cfg_db):
    cfg, db = cfg_db
    item = AnalyzedUpdate(
        update=Update(source="docker", subject="x", current_version="1", new_version="2"),
        analysis=Analysis(severity=Severity.HIGH, summary="hi"),
        status=UpdateStatus.ANALYZED,
        detected_at=datetime(2026, 6, 1),
    )
    db.upsert(item)
    resp = dispatch(cfg, db, {
        "jsonrpc": "2.0", "method": "tools/call", "id": 1,
        "params": {"name": "list_updates"},
    })
    data = resp["result"]["structuredContent"]
    assert data["count"] == 1
    assert data["items"][0]["subject"] == "x"
    assert data["items"][0]["severity"] == "high"


def test_dispatch_get_update_requires_id(cfg_db):
    cfg, db = cfg_db
    resp = dispatch(cfg, db, {
        "jsonrpc": "2.0", "method": "tools/call", "id": 1,
        "params": {"name": "get_update", "arguments": {}},
    })
    assert resp["error"]["code"] == -32602
    assert "update_id" in resp["error"]["message"]


def test_dispatch_get_update_unknown_id_returns_validation_error(cfg_db):
    cfg, db = cfg_db
    resp = dispatch(cfg, db, {
        "jsonrpc": "2.0", "method": "tools/call", "id": 1,
        "params": {"name": "get_update", "arguments": {"update_id": "nope"}},
    })
    assert resp["error"]["code"] == -32602


def test_dispatch_set_update_status_writes(cfg_db):
    cfg, db = cfg_db
    item = AnalyzedUpdate(
        update=Update(source="docker", subject="x", current_version="1", new_version="2"),
        analysis=Analysis(severity=Severity.HIGH, summary="hi"),
        status=UpdateStatus.ANALYZED,
        detected_at=datetime(2026, 6, 1),
    )
    db.upsert(item)
    resp = dispatch(cfg, db, {
        "jsonrpc": "2.0", "method": "tools/call", "id": 1,
        "params": {"name": "set_update_status",
                   "arguments": {"update_id": item.id, "status": "applied"}},
    })
    assert resp["result"]["structuredContent"]["status"] == "applied"
    assert db.get(item.id).status == UpdateStatus.APPLIED


def test_dispatch_list_watched_returns_count(cfg_db):
    cfg, db = cfg_db
    db.add_watched_repo("owner/repo", nickname="x")
    resp = dispatch(cfg, db, {
        "jsonrpc": "2.0", "method": "tools/call", "id": 1,
        "params": {"name": "list_watched_repos"},
    })
    assert resp["result"]["structuredContent"]["count"] == 1


def test_dispatch_list_pending_dispatches_empty_by_default(cfg_db):
    cfg, db = cfg_db
    resp = dispatch(cfg, db, {
        "jsonrpc": "2.0", "method": "tools/call", "id": 1,
        "params": {"name": "list_pending_dispatches"},
    })
    assert resp["result"]["structuredContent"]["count"] == 0


def test_dispatch_unknown_tool_returns_minus_32601(cfg_db):
    cfg, db = cfg_db
    resp = dispatch(cfg, db, {
        "jsonrpc": "2.0", "method": "tools/call", "id": 1,
        "params": {"name": "nonexistent_tool"},
    })
    assert resp["error"]["code"] == -32601


# ─── HTTP integration ────────────────────────────────────────────────────


def test_mcp_post_endpoint_handles_jsonrpc_envelope(tmp_path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    cfg.web.auth.enabled = False
    cfg.scheduler.enabled = False
    app = web.create_app(cfg)
    with TestClient(app) as c:
        r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": 7})
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == 7
        assert "result" in body
        assert len(body["result"]["tools"]) == len(TOOLS)


def test_mcp_info_get_returns_tool_names(tmp_path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    cfg.web.auth.enabled = False
    cfg.scheduler.enabled = False
    app = web.create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/mcp/info")
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "homelabsage"
        assert body["protocol"] == "mcp/json-rpc-2.0"
        assert "health" in body["tools"]
