"""Tests for v0.6.6: MCP audit tools, env-diff, LLM health gate,
audit SSE stream."""

from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

from homelabsage.config import Config
from homelabsage.env_diff import EnvDiffEntry, _is_credential_like
from homelabsage.env_diff import diff as env_diff
from homelabsage.llm_health import _candidate_urls
from homelabsage.llm_health import probe as probe_llm

# ─── env_diff ──────────────────────────────────────────────────────


def test_env_diff_new_credential_high_severity():
    out = env_diff(
        container_env=["FOO=1"],
        image_env=["FOO=1", "API_TOKEN="],
    )
    new = [e for e in out if e.kind == "new"]
    assert len(new) == 1
    assert new[0].name == "API_TOKEN"
    assert new[0].severity == "high"


def test_env_diff_new_normal_medium_severity():
    out = env_diff(
        container_env=["FOO=1"],
        image_env=["FOO=1", "TIMEZONE=UTC"],
    )
    new = [e for e in out if e.kind == "new"]
    assert len(new) == 1
    assert new[0].severity == "medium"


def test_env_diff_removed_info():
    out = env_diff(
        container_env=["FOO=1", "BAR=2"],
        image_env=["FOO=1"],
    )
    rem = [e for e in out if e.kind == "removed"]
    assert len(rem) == 1
    assert rem[0].name == "BAR"
    assert rem[0].severity == "info"


def test_env_diff_changed_default():
    out = env_diff(
        container_env=["FOO=oldvalue"],
        image_env=["FOO=newvalue"],
    )
    ch = [e for e in out if e.kind == "changed"]
    assert len(ch) == 1
    assert ch[0].container_value == "oldvalue"
    assert ch[0].image_value == "newvalue"


def test_env_diff_ignores_path_and_friends():
    out = env_diff(
        container_env=["PATH=/usr/bin", "HOME=/root", "FOO=1"],
        image_env=["PATH=/something/else", "HOSTNAME=abc"],
    )
    names = {e.name for e in out}
    assert "PATH" not in names
    assert "HOME" not in names
    assert "HOSTNAME" not in names


def test_env_diff_handles_empty_inputs():
    assert env_diff([], []) == []
    assert env_diff(None, None) == []


def test_env_diff_to_context_shape():
    e = EnvDiffEntry(
        name="X", kind="new", container_value=None,
        image_value="v", severity="medium",
    )
    ctx = e.to_context()
    assert ctx == {
        "name": "X", "kind": "new", "container_value": None,
        "image_value": "v", "severity": "medium",
    }


def test_env_diff_credential_detection():
    assert _is_credential_like("DB_PASSWORD")
    assert _is_credential_like("MY_TOKEN")
    assert _is_credential_like("API_KEY")
    assert not _is_credential_like("PORT")


# ─── llm_health ────────────────────────────────────────────────────


def test_llm_health_empty_endpoint_treated_as_ok():
    out = probe_llm("")
    assert out.ok
    assert "no endpoint" in out.reason


def test_llm_health_candidate_urls_for_v1_endpoint():
    urls = _candidate_urls("https://api.example.com/v1")
    assert any("/models" in u for u in urls)
    assert any(u.endswith("/health") for u in urls)


def test_llm_health_candidate_urls_for_base_endpoint():
    urls = _candidate_urls("http://192.168.31.19:11434")
    assert any("/v1/models" in u for u in urls)


def test_llm_health_candidate_urls_strips_chat_completions():
    urls = _candidate_urls("https://api.example.com/v1/chat/completions")
    assert any(u.endswith("/v1/models") for u in urls)


def test_llm_health_probe_unreachable_returns_not_ok():
    # localhost port 1 is reserved/never bound — connect refused
    out = probe_llm(
        "http://127.0.0.1:1", timeout=1.0,
    )
    assert not out.ok
    assert out.reason


def test_llm_health_probe_treats_401_as_alive(monkeypatch):
    """Auth failure means the endpoint is up; user's auth is the issue."""
    class _Resp:
        status_code = 401
    class _Client:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, _u): return _Resp()
    monkeypatch.setattr(httpx, "Client", _Client)
    out = probe_llm("https://api.example.com/v1", api_key="bad")
    assert out.ok
    assert out.status_code == 401


# ─── MCP tools ────────────────────────────────────────────────────


def test_mcp_disk_pressure_check_no_paths():
    from homelabsage.db import Database
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["disk_pressure_check"]["impl"]
    out = impl(cfg, db, {})
    assert out["count"] == 0
    assert "no paths" in out["reason"]


def test_mcp_disk_pressure_check_with_real_path(tmp_path):
    from homelabsage.db import Database
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["disk_pressure_check"]["impl"]
    out = impl(cfg, db, {"paths": [str(tmp_path)]})
    # Real fs — count could be 0 (lots free) or >0 (filling up). Both OK.
    assert "count" in out
    assert "items" in out


def test_mcp_compose_overrides_finds_pair(tmp_path):
    from homelabsage.db import Database
    from homelabsage.mcp import TOOLS
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "docker-compose.override.yml").write_text("services: {}\n")
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["compose_overrides"]["impl"]
    out = impl(cfg, db, {"paths": [str(tmp_path)]})
    assert out["count"] == 1


def test_mcp_tls_check_run_no_urls():
    from homelabsage.db import Database
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["tls_check_run"]["impl"]
    out = impl(cfg, db, {})
    assert out["count"] == 0
    assert "no URLs" in out["reason"]


def test_mcp_tls_check_run_invalid_url_returns_failure():
    from homelabsage.db import Database
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["tls_check_run"]["impl"]
    out = impl(cfg, db, {"urls": ["ftp://x.example/"]})
    assert out["count"] == 1
    assert out["items"][0]["ok"] is False


# ─── SSE audit stream ─────────────────────────────────────────────


def test_audit_stream_emits_done_event(tmp_path):
    from homelabsage.web import create_app
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "test.db")
    app = create_app(cfg)
    client = TestClient(app)
    with client.stream("GET", "/api/audit/stream") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = b"".join(r.iter_bytes())
    text = body.decode()
    # `done` event always emitted, carries summary JSON
    assert "event: done" in text
    # Extract the done payload (last event)
    last_data = text.rsplit("data: ", 1)[-1].strip()
    summary = json.loads(last_data)
    assert "healthy" in summary
    assert "counts_by_severity" in summary
