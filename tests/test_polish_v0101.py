"""Tests for v0.10.1: punch-list crits C2 + C5 + I10 in MCP."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from homelabsage.config import Config
from homelabsage.db import Database
from homelabsage.mcp import dispatch
from homelabsage.web import create_app


def _client(tmp_path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    return TestClient(create_app(cfg)), Database(cfg.storage.database_path), cfg


# ─── C5: catch-all sanitizes exception messages ───────────────────


def test_dispatch_catchall_returns_safe_error_only(monkeypatch, tmp_path):
    """A tool that crashes with a secret-bearing exception must not echo
    `str(e)` in the JSON-RPC error.message."""
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)

    # Monkey-patch one tool to raise with a secret in the message.
    from homelabsage import mcp as mcp_mod
    secret_msg = "connect failed to http://user:VERYSECRETTOKEN@halo.local/api"

    def _boom(_cfg, _db, _p):
        raise RuntimeError(secret_msg)

    original = mcp_mod.TOOLS["version"]["impl"]
    mcp_mod.TOOLS["version"]["impl"] = _boom
    try:
        out = dispatch(cfg, db, {
            "jsonrpc": "2.0", "id": 1,
            "method": "tools/call",
            "params": {"name": "version", "arguments": {}},
        })
    finally:
        mcp_mod.TOOLS["version"]["impl"] = original

    err = out["error"]
    assert err["code"] == -32603
    assert "VERYSECRETTOKEN" not in err["message"]
    assert "halo.local" not in err["message"]
    # Should mention exception type (so user knows what happened) + log hint
    assert "RuntimeError" in err["message"]
    assert "server logs" in err["message"]


def test_dispatch_value_error_still_passes_message(tmp_path):
    """ValueError is the contract-violation path — its message is part
    of the tool API and should still surface for the caller."""
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    from homelabsage import mcp as mcp_mod

    def _bad(_cfg, _db, _p):
        raise ValueError("limit must be 1..500")

    original = mcp_mod.TOOLS["version"]["impl"]
    mcp_mod.TOOLS["version"]["impl"] = _bad
    try:
        out = dispatch(cfg, db, {
            "jsonrpc": "2.0", "id": 1,
            "method": "tools/call",
            "params": {"name": "version", "arguments": {}},
        })
    finally:
        mcp_mod.TOOLS["version"]["impl"] = original

    err = out["error"]
    assert err["code"] == -32602
    assert "limit must be 1..500" in err["message"]


# ─── I10: tls_check_run caps arbitrary URL list ───────────────────


def test_tls_check_run_caps_url_list(monkeypatch, tmp_path):
    """Caller passing 1000 URLs only gets the first 50 probed."""
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)

    seen_count: list[int] = []

    def _fake_check_urls(urls, **_kw):
        seen_count.append(len(urls))
        return []

    import homelabsage.tls_check as tls_mod
    monkeypatch.setattr(tls_mod, "check_urls", _fake_check_urls)

    out = dispatch(cfg, db, {
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {
            "name": "tls_check_run",
            "arguments": {"urls": [f"https://h{i}.example" for i in range(1000)]},
        },
    })
    assert "result" in out
    assert seen_count == [50]


# ─── C2: HTTP /mcp endpoint runs blocking tool off the loop ───────


def test_mcp_post_does_not_block_event_loop(tmp_path):
    """Even when a tool sleeps for 0.3s, the HTTP layer schedules it on
    a worker thread so other requests can interleave. We assert by
    counting concurrent successful requests in a tight window."""
    import threading

    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    client = TestClient(create_app(cfg))
    from homelabsage import mcp as mcp_mod

    def _slow(_c, _d, _p):
        time.sleep(0.3)
        return {"ok": True}

    original = mcp_mod.TOOLS["version"]["impl"]
    mcp_mod.TOOLS["version"]["impl"] = _slow
    try:
        results: list[int] = []

        def hit():
            r = client.post("/mcp", json={
                "jsonrpc": "2.0", "id": 1,
                "method": "tools/call",
                "params": {"name": "version", "arguments": {}},
            })
            results.append(r.status_code)

        start = time.monotonic()
        threads = [threading.Thread(target=hit) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.monotonic() - start
    finally:
        mcp_mod.TOOLS["version"]["impl"] = original

    assert results == [200, 200, 200]
    # 3 requests × 0.3s serial = 0.9s; concurrent should be < 0.7s.
    # Loose bound so CI jitter doesn't flake.
    assert elapsed < 0.75, f"expected <0.75s, got {elapsed:.2f}s"


def test_mcp_post_basic_tools_call_works(tmp_path):
    """Sanity: round-trip the to_thread wrap doesn't break tools/call."""
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    client = TestClient(create_app(cfg))
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 7,
        "method": "tools/list", "params": {},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == 7
    assert "tools" in body["result"]
    assert len(body["result"]["tools"]) > 10
