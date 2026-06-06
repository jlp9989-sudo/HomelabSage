"""Tests for v0.6.2: restart_freq, exposed_ports, Slack output, API-key
auth, plus regression tests for the review-found CRITICAL bugs."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from homelabsage import web
from homelabsage.config import (
    AppriseOutputConfig,  # noqa: F401  imported for completeness
    SlackOutputConfig,
    load_config,
)
from homelabsage.exposed_ports import PortFinding, _is_private_or_loopback
from homelabsage.exposed_ports import evaluate as ports_eval
from homelabsage.models import Analysis, AnalyzedUpdate, Severity, Update
from homelabsage.outputs.slack import SlackOutput
from homelabsage.restart_freq import RestartFinding, _severity
from homelabsage.restart_freq import evaluate as rf_eval

# ─── restart_freq ───────────────────────────────────────────────────


def test_restart_freq_severity_buckets():
    assert _severity(0.1) == "info"
    assert _severity(0.5) == "medium"
    assert _severity(10) == "high"
    assert _severity(120) == "critical"


def test_restart_freq_below_min_count_returns_none():
    now = datetime(2026, 6, 1, tzinfo=UTC)
    started = now - timedelta(hours=24)
    assert rf_eval(restart_count=2, started_at=started, now=now) is None


def test_restart_freq_no_started_at_returns_none():
    now = datetime(2026, 6, 1, tzinfo=UTC)
    assert rf_eval(restart_count=99, started_at=None, now=now) is None


def test_restart_freq_just_started_returns_none():
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    started = now - timedelta(seconds=30)
    assert rf_eval(restart_count=5, started_at=started, now=now) is None


def test_restart_freq_medium_after_steady_flapping():
    """6 restarts over 4 hours → 1.5/hr → high."""
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    started = now - timedelta(hours=4)
    f = rf_eval(restart_count=6, started_at=started, now=now)
    assert isinstance(f, RestartFinding)
    assert f.severity == "high"


def test_restart_freq_handles_naive_timestamps():
    now = datetime(2026, 6, 1, 12, 0)
    started = now - timedelta(hours=2)
    f = rf_eval(restart_count=5, started_at=started, now=now)
    assert f is not None


# ─── exposed_ports ──────────────────────────────────────────────────


def test_is_private_loopback():
    assert _is_private_or_loopback("127.0.0.1")
    assert _is_private_or_loopback("192.168.1.10")
    assert _is_private_or_loopback("10.0.0.5")
    assert _is_private_or_loopback("172.16.0.1")
    assert not _is_private_or_loopback("0.0.0.0")
    assert not _is_private_or_loopback("8.8.8.8")
    # Empty/wildcard is "wants flagging"
    assert not _is_private_or_loopback("")


def test_ports_eval_flags_privileged_wildcard():
    settings = {
        "Ports": {
            "80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "80"}],
        },
    }
    findings = ports_eval(settings)
    assert len(findings) == 1
    assert findings[0].reason == "privileged_wildcard"
    assert findings[0].container_port == 80


def test_ports_eval_skips_loopback_privileged():
    """127.0.0.1:80 isn't a public surface."""
    settings = {"Ports": {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "80"}]}}
    assert ports_eval(settings) == []


def test_ports_eval_skips_unprivileged_wildcard():
    """0.0.0.0:8080 is the homelab norm — not flagged."""
    settings = {"Ports": {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]}}
    assert ports_eval(settings) == []


def test_ports_eval_flags_public_bind_on_high_port():
    settings = {
        "Ports": {
            "8080/tcp": [{"HostIp": "8.8.8.8", "HostPort": "8080"}],
        },
    }
    findings = ports_eval(settings)
    assert len(findings) == 1
    assert findings[0].reason == "public_bind"


def test_ports_eval_skips_none_bindings():
    """`EXPOSE` only — not host-published — should not flag."""
    settings = {"Ports": {"80/tcp": None, "8080/tcp": None}}
    assert ports_eval(settings) == []


def test_ports_eval_handles_missing_fields():
    assert ports_eval({}) == []
    assert ports_eval(None) == []
    assert ports_eval({"Ports": "not-a-dict"}) == []


def test_port_finding_to_context():
    f = PortFinding(80, "0.0.0.0", 80, "tcp", "privileged_wildcard")
    assert f.to_context()["container_port"] == 80
    assert f.to_context()["reason"] == "privileged_wildcard"


# ─── Slack output ───────────────────────────────────────────────────


def test_slack_skips_when_disabled():
    cfg = SlackOutputConfig(enabled=False, webhook_url="http://x")
    out = SlackOutput(cfg)
    item = AnalyzedUpdate(
        update=Update(source="x", subject="y",
                      current_version="1", new_version="2"),
        analysis=Analysis(severity=Severity.HIGH, summary="m"),
    )
    asyncio.run(out.send(item))


def test_slack_payload_has_blocks_and_attachments():
    cfg = SlackOutputConfig(
        enabled=True,
        webhook_url="https://hooks.slack.com/services/T/B/SECRET",
    )
    out = SlackOutput(cfg)
    item = AnalyzedUpdate(
        update=Update(source="docker", subject="mealie",
                      current_version="1", new_version="2",
                      release_url="https://example/r"),
        analysis=Analysis(
            severity=Severity.CRITICAL, summary="major",
            breaking_changes=["env A→B"],
            recommended_action="back up",
        ),
    )
    payload = out._build_payload(item)
    assert "blocks" in payload
    assert payload["attachments"][0]["color"]
    # Header block carries the title
    headers = [b for b in payload["blocks"] if b.get("type") == "header"]
    assert headers
    # Breaking changes block present
    text_bits = " ".join(
        str(b) for b in payload["blocks"]
    )
    assert "env A→B" in text_bits
    assert "back up" in text_bits


def test_slack_respects_min_severity():
    cfg = SlackOutputConfig(
        enabled=True,
        webhook_url="https://hooks.slack.com/services/T/B/S",
        min_severity="high",
    )
    out = SlackOutput(cfg)
    low = AnalyzedUpdate(
        update=Update(source="x", subject="y",
                      current_version="1", new_version="2"),
        analysis=Analysis(severity=Severity.MEDIUM, summary="m"),
    )
    assert not out._should_send(low)


# ─── API-key auth ───────────────────────────────────────────────────


def _make_app_auth(tmp_path: Path, api_keys: list[str]) -> Path:
    body = (
        "llm:\n  provider: disabled\n  model: stub\n  endpoint: http://stub\n"
        "scheduler:\n  enabled: false\n"
        f"storage:\n  database_path: {tmp_path}/state.sqlite\n"
        "web:\n  enabled: true\n  host: 127.0.0.1\n  port: 0\n"
        "  auth:\n    enabled: true\n    username: javi\n"
        "    password: secret\n"
    )
    if api_keys:
        body += "    api_keys:\n"
        for k in api_keys:
            body += f'      - "{k}"\n'
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(body)
    return cfg_path


def test_api_key_bearer_grants_access(tmp_path: Path):
    cfg_path = _make_app_auth(tmp_path, ["my-headless-agent-token"])
    cfg = load_config(cfg_path)
    app = web.create_app(cfg, cfg_path=cfg_path)
    with TestClient(app) as client:
        # No auth → 401
        r = client.get("/api/updates")
        assert r.status_code == 401
        # Bearer → 200
        r = client.get(
            "/api/updates",
            headers={"Authorization": "Bearer my-headless-agent-token"},
        )
        assert r.status_code == 200


def test_api_key_wrong_bearer_rejects(tmp_path: Path):
    cfg_path = _make_app_auth(tmp_path, ["real-key"])
    cfg = load_config(cfg_path)
    app = web.create_app(cfg, cfg_path=cfg_path)
    with TestClient(app) as client:
        r = client.get(
            "/api/updates",
            headers={"Authorization": "Bearer impostor"},
        )
        assert r.status_code == 401


def test_basic_auth_still_works_alongside_bearer(tmp_path: Path):
    cfg_path = _make_app_auth(tmp_path, ["k"])
    cfg = load_config(cfg_path)
    app = web.create_app(cfg, cfg_path=cfg_path)
    with TestClient(app) as client:
        r = client.get("/api/updates", auth=("javi", "secret"))
        assert r.status_code == 200


def test_auth_bypass_paths_are_exact():
    """Regression: typos in the bypass list don't open new paths."""
    from homelabsage.web.auth import (
        AUTH_BYPASS_EXACT,
        AUTH_BYPASS_PREFIX,
        _is_bypassed,
    )
    assert "/healthz" in AUTH_BYPASS_EXACT
    assert "/metrics" in AUTH_BYPASS_EXACT
    assert "/api/stack-health" in AUTH_BYPASS_EXACT
    assert "/widget/" in AUTH_BYPASS_PREFIX
    # Typo doesn't match
    assert not _is_bypassed("/api/stack_health")
    assert not _is_bypassed("/metric")
    # Correct paths match
    assert _is_bypassed("/healthz")
    assert _is_bypassed("/widget/homepage")
    # Random path is NOT bypassed
    assert not _is_bypassed("/api/updates")


# ─── inbox: review-found CRITICAL fixes ─────────────────────────────


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


def test_inbox_rejects_javascript_url(tmp_path: Path):
    """Regression: stored-XSS via release_url. http(s) only now."""
    app = _bare_app(tmp_path)
    with TestClient(app) as client:
        r = client.post(
            "/api/inbox/external",
            json={
                "subject": "x", "new_version": "2",
                "release_url": "javascript:alert(1)",
            },
        )
        assert r.status_code == 400


def test_inbox_rejects_file_url(tmp_path: Path):
    app = _bare_app(tmp_path)
    with TestClient(app) as client:
        r = client.post(
            "/api/inbox/external",
            json={
                "subject": "x", "new_version": "2",
                "release_url": "file:///etc/passwd",
            },
        )
        assert r.status_code == 400


def test_inbox_accepts_http_https(tmp_path: Path):
    app = _bare_app(tmp_path)
    with TestClient(app) as client:
        for url in ("http://x.example/r", "https://x.example/r"):
            r = client.post(
                "/api/inbox/external",
                json={"subject": f"x-{url[-1]}", "new_version": "2",
                      "release_url": url},
            )
            assert r.status_code == 200


def test_inbox_truncates_release_notes_to_64k(tmp_path: Path):
    """Regression: 100 MB POST used to bloat the DB. Now 64 KB cap."""
    app = _bare_app(tmp_path)
    big = "x" * 200_000  # 200 KB of release notes
    with TestClient(app) as client:
        r = client.post(
            "/api/inbox/external",
            json={
                "subject": "huge", "new_version": "2",
                "release_notes": big,
            },
        )
        assert r.status_code == 200
    # Read back the row to confirm truncation
    from homelabsage.db import Database
    db = Database(tmp_path / "state.sqlite")
    try:
        item = db.get("external:huge:2")
        assert item is not None
        assert len(item.update.release_notes or "") == 65536
    finally:
        db.close()


def test_inbox_rejects_overlong_subject(tmp_path: Path):
    app = _bare_app(tmp_path)
    with TestClient(app) as client:
        r = client.post(
            "/api/inbox/x",
            json={"subject": "x" * 500, "new_version": "2"},
        )
        assert r.status_code == 400


# ─── webhook output header-injection guard ──────────────────────────


def test_webhook_drops_headers_with_crlf(monkeypatch):
    from homelabsage.config import WebhookOutputConfig
    from homelabsage.outputs.webhook import WebhookOutput
    cfg = WebhookOutputConfig(
        enabled=True, url="http://x.example",
        headers={"X-Bad": "value\r\nX-Injected: pwned"},
    )
    out = WebhookOutput(cfg)
    item = AnalyzedUpdate(
        update=Update(source="x", subject="y",
                      current_version="1", new_version="2"),
        analysis=Analysis(severity=Severity.HIGH, summary="m"),
    )
    captured: dict = {}

    class FakeResp:
        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            captured["headers"] = headers
            return FakeResp()

    import homelabsage.outputs as outputs_mod
    monkeypatch.setattr(outputs_mod.httpx, "AsyncClient", FakeClient)
    asyncio.run(out.send(item))
    assert "X-Bad" not in captured["headers"]
    assert "X-Injected" not in captured["headers"]


# ─── MCP analyze_url no longer crashes inside event loop ────────────


def test_mcp_run_coro_helper_handles_nested_loop():
    """The MCP impls used to `asyncio.run()` from inside the FastAPI
    async handler; now `_run_coro` detects the running loop and
    schedules on a worker thread."""
    from homelabsage.mcp_tools import _run_coro

    async def _async_op():
        return 42

    # No outer loop — runs via asyncio.run
    assert _run_coro(_async_op()) == 42

    async def _outer():
        # Inside an outer loop — _run_coro must NOT raise
        # "cannot be called from a running event loop"
        return _run_coro(_async_op())

    assert asyncio.run(_outer()) == 42
