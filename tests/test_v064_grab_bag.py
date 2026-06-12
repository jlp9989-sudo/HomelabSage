"""Tests for v0.6.4: TLS cert probe, MS Teams output, volume orphans,
stack-export CLI."""

from __future__ import annotations

import asyncio
import ssl

from homelabsage.config import MSTeamsOutputConfig
from homelabsage.models import Analysis, AnalyzedUpdate, Severity, Update
from homelabsage.outputs.msteams import MSTeamsOutput, _build_adaptive_card
from homelabsage.tls_check import (
    TLSCheck,
    _parse_x509_date,
    _severity,
    probe,
)

# ─── tls_check ──────────────────────────────────────────────────────


def test_severity_buckets():
    assert _severity(60, warn_days=30) == "info"
    assert _severity(20, warn_days=30) == "medium"
    assert _severity(5, warn_days=30) == "high"
    assert _severity(0, warn_days=30) == "critical"
    assert _severity(-3, warn_days=30) == "critical"
    assert _severity(None, warn_days=30) == "high"


def test_parse_x509_date_openssl_format():
    out = _parse_x509_date("Jun 15 12:00:00 2026 GMT")
    assert out is not None
    assert out.year == 2026
    assert out.tzinfo is not None


def test_parse_x509_date_garbage_returns_none():
    assert _parse_x509_date("not a date") is None
    assert _parse_x509_date("") is None


def test_probe_empty_url():
    out = probe("")
    assert not out.ok
    assert "empty" in out.reason


def test_probe_http_is_no_tls():
    out = probe("http://x.example/")
    assert out.ok
    assert "no TLS" in out.reason


def test_probe_unsupported_scheme():
    out = probe("ftp://x.example/")
    assert not out.ok
    assert "unsupported scheme" in out.reason


def test_probe_handles_dns_failure(monkeypatch):
    import socket as _sock
    def boom(*args, **kwargs):
        raise _sock.gaierror("name doesn't resolve")
    monkeypatch.setattr(_sock, "create_connection", boom)
    out = probe("https://invalid.invalid/")
    assert not out.ok
    assert out.severity == "high"


def test_probe_handles_tls_handshake_failure(monkeypatch):
    """SSLError raised during handshake → ok=False, severity high."""
    class FakeSock:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_create_conn(*a, **kw): return FakeSock()
    class FakeCtx:
        def wrap_socket(self, *a, **kw):
            raise ssl.SSLError("handshake failed")
    import socket as _sock
    monkeypatch.setattr(_sock, "create_connection", fake_create_conn)
    monkeypatch.setattr(
        ssl, "create_default_context", lambda: FakeCtx(),
    )
    out = probe("https://x.example/")
    assert not out.ok
    assert "tls:" in out.reason


def test_tls_check_to_context_round_trip():
    c = TLSCheck(url="https://x", ok=True, reason="ok",
                 days_until_expiry=42, not_after="2026-07-15T00:00:00+00:00",
                 issuer="Let's Encrypt", severity="info")
    ctx = c.to_context()
    assert ctx["url"] == "https://x"
    assert ctx["days_until_expiry"] == 42


# ─── ms teams ───────────────────────────────────────────────────────


def test_msteams_skips_when_disabled():
    cfg = MSTeamsOutputConfig(enabled=False, webhook_url="http://x")
    out = MSTeamsOutput(cfg)
    item = AnalyzedUpdate(
        update=Update(source="x", subject="y",
                      current_version="1", new_version="2"),
        analysis=Analysis(severity=Severity.HIGH, summary="m"),
    )
    asyncio.run(out.send(item))


def test_msteams_skips_when_no_url():
    cfg = MSTeamsOutputConfig(enabled=True, webhook_url="")
    out = MSTeamsOutput(cfg)
    item = AnalyzedUpdate(
        update=Update(source="x", subject="y",
                      current_version="1", new_version="2"),
        analysis=Analysis(severity=Severity.HIGH, summary="m"),
    )
    assert not out._should_send(item)


def test_msteams_card_shape():
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
    payload = _build_adaptive_card(item)
    assert payload["type"] == "message"
    card = payload["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard"
    assert card["version"] == "1.5"
    text_blocks = [b for b in card["body"] if b.get("type") == "TextBlock"]
    text = " ".join(b.get("text", "") for b in text_blocks)
    assert "mealie" in text
    assert "1 → 2" in text
    assert "env A→B" in text
    assert "back up" in text
    # Action button for release notes
    assert any(
        a.get("title") == "Release notes" for a in card.get("actions", [])
    )


def test_msteams_respects_min_severity():
    cfg = MSTeamsOutputConfig(
        enabled=True, webhook_url="https://example/webhook",
        min_severity="high",
    )
    out = MSTeamsOutput(cfg)
    low = AnalyzedUpdate(
        update=Update(source="x", subject="y",
                      current_version="1", new_version="2"),
        analysis=Analysis(severity=Severity.MEDIUM, summary="m"),
    )
    assert not out._should_send(low)


