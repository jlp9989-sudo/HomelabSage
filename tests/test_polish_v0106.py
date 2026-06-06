"""Tests for v0.10.6: I8 (webhook context redact) + I11 (KV-redact false-positives)."""

from __future__ import annotations

import asyncio

import httpx

from homelabsage.config import WebhookOutputConfig
from homelabsage.models import Analysis, AnalyzedUpdate, Severity, Update
from homelabsage.outputs.webhook import WebhookOutput
from homelabsage.secret_guard import redact_text

# ─── I11: KV redact no longer over-fires on config flags ──────────


def test_kv_redact_skips_safe_boolean_value():
    out, _r = redact_text("OBSERVABILITY_PRIVATE=true")
    assert "true" in out
    assert "redacted" not in out.lower()


def test_kv_redact_skips_auth_public():
    out, _r = redact_text("AUTH=public")
    assert "public" in out
    assert "redacted" not in out.lower()


def test_kv_redact_skips_session_on():
    out, _r = redact_text("SESSION=on")
    assert "on" in out
    assert "redacted" not in out.lower()


def test_kv_redact_skips_short_numeric():
    """Port-like values stay visible — they're config, not secrets."""
    out, _r = redact_text("AUTH_PORT=8080")
    assert "8080" in out
    assert "redacted" not in out.lower()


def test_kv_redact_still_fires_on_real_password():
    out, _r = redact_text("PASSWORD=hunter2longer")
    assert "hunter2longer" not in out
    assert "redacted" in out.lower()


def test_kv_redact_still_fires_on_ghp_token():
    """The inline pattern catches ghp_…, BUT the KV-line pattern fires
    first when the prefix matches. Either way the token must be gone."""
    leaky = "GITHUB_TOKEN=ghp_AbCdEfGhIjKlMnOpQrStUvWxYz123456"
    out, _r = redact_text(leaky)
    assert "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz123456" not in out


def test_kv_redact_skips_case_insensitive():
    out, _r = redact_text("AUTH=PUBLIC")
    assert "PUBLIC" in out
    assert "redacted" not in out.lower()


def test_kv_redact_quoted_safe_value():
    """`AUTH="true"` (with quotes) should also be treated as safe."""
    out, _r = redact_text('AUTH="true"')
    assert "true" in out
    assert "redacted" not in out.lower()


# ─── I8: webhook output scrubs context dict + analyzer text ───────


def _make_item_with_leaky_context():
    return AnalyzedUpdate(
        update=Update(
            source="docker", subject="mealie",
            current_version="1", new_version="2",
            context={
                "puid_pgid": "1000:1000",
                "env": {
                    "DB_PASSWORD": "supersecret",
                    "GITHUB_TOKEN": "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz123456",
                    "LOG_LEVEL": "info",
                },
                "release_notes": "Run with --token=ghp_abcdefghijklmnopqrst123",
            },
        ),
        analysis=Analysis(
            severity=Severity.HIGH,
            summary="Update OK. GITHUB_TOKEN=ghp_AbCdEfGhIjKlMnOpQrStUvWxYz123456 noted.",
            breaking_changes=["uses API_KEY=secretvalue_long_string"],
            recommended_action="set DB_PASSWORD=verysecretlong",
        ),
    )


def _capture_post(monkeypatch):
    captured: list[dict] = []

    class _OK:
        status_code = 200
        def json(self): return {}
        def raise_for_status(self): return None

    class _MockClient:
        def __init__(self, *_a, **_kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
        async def post(self, url, **kw):
            captured.append({"url": url, "json": kw.get("json")})
            return _OK()

    monkeypatch.setattr(httpx, "AsyncClient", _MockClient)
    return captured


def test_webhook_redacts_env_secrets_in_context(monkeypatch):
    cfg = WebhookOutputConfig(
        enabled=True, url="https://hook.example.com/x", min_severity="info",
    )
    out = WebhookOutput(cfg)
    item = _make_item_with_leaky_context()
    captured = _capture_post(monkeypatch)
    asyncio.run(out.send(item))

    body = captured[0]["json"]
    payload_str = repr(body)  # quick "anywhere in tree" check
    assert "supersecret" not in payload_str
    assert "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz123456" not in payload_str


def test_webhook_redacts_analyzer_text(monkeypatch):
    cfg = WebhookOutputConfig(
        enabled=True, url="https://hook.example.com/x", min_severity="info",
    )
    out = WebhookOutput(cfg)
    item = _make_item_with_leaky_context()
    captured = _capture_post(monkeypatch)
    asyncio.run(out.send(item))

    body = captured[0]["json"]
    assert "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz123456" not in body["summary"]
    assert "verysecretlong" not in body["recommended_action"]
    for bc in body["breaking_changes"]:
        assert "secretvalue_long_string" not in bc


def test_webhook_preserves_safe_config_values(monkeypatch):
    """Config flags in env (LOG_LEVEL=info) must survive the redact."""
    cfg = WebhookOutputConfig(
        enabled=True, url="https://hook.example.com/x", min_severity="info",
    )
    out = WebhookOutput(cfg)
    item = _make_item_with_leaky_context()
    captured = _capture_post(monkeypatch)
    asyncio.run(out.send(item))

    body = captured[0]["json"]
    payload_str = repr(body)
    assert "LOG_LEVEL" in payload_str  # key preserved
    assert "info" in payload_str       # value preserved (safe flag)
    assert "puid_pgid" in payload_str  # non-secret structural data preserved


def test_webhook_disabled_short_circuits(monkeypatch):
    cfg = WebhookOutputConfig(enabled=False, url="https://x", min_severity="info")
    out = WebhookOutput(cfg)
    item = _make_item_with_leaky_context()
    captured = _capture_post(monkeypatch)
    asyncio.run(out.send(item))
    assert captured == []
