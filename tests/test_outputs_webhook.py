"""Unit tests for Discord / Ntfy / Gotify outputs.

The three outputs share the same shape (severity gate + httpx POST), so we
test the gate logic per-output and verify the wire payload of one send for
each. Real network calls are stubbed with `respx` would be heavier; we
monkeypatch `httpx.AsyncClient.post` directly to capture call arguments.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
import pytest

from homelabsage.config import (
    DiscordOutputConfig,
    GotifyOutputConfig,
    NtfyOutputConfig,
)
from homelabsage.models import (
    Analysis,
    AnalyzedUpdate,
    Severity,
    Update,
    UpdateStatus,
)
from homelabsage.outputs.discord import DiscordOutput
from homelabsage.outputs.gotify import GotifyOutput
from homelabsage.outputs.ntfy import NtfyOutput


def _analyzed(
    severity: Severity = Severity.HIGH,
    breaking: list[str] | None = None,
    action: str | None = "restart container after upgrade",
) -> AnalyzedUpdate:
    return AnalyzedUpdate(
        update=Update(
            source="docker",
            subject="openclaw",
            current_version="4.15.0",
            new_version="5.7.0",
            release_url="https://example.com/r/5.7.0",
            release_notes="changelog body",
        ),
        analysis=Analysis(
            severity=severity,
            summary="Major refactor; sessions table migrates on first start.",
            breaking_changes=breaking or [],
            recommended_action=action,
        ),
        status=UpdateStatus.ANALYZED,
        detected_at=datetime.utcnow(),
    )


class _RecordingResponse:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code
        self.text = ""

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "boom", request=httpx.Request("POST", "http://x"), response=self  # type: ignore[arg-type]
            )


class _RecordingClient:
    def __init__(self, *a: Any, **kw: Any) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> _RecordingClient:
        return self

    async def __aexit__(self, *a: Any) -> None:
        pass

    async def post(self, url: str, **kw: Any) -> _RecordingResponse:
        self.calls.append({"url": url, **kw})
        return _RecordingResponse(200)


@pytest.fixture
def capture(monkeypatch):
    """Replace `httpx.AsyncClient` with a recorder that captures every POST.

    Outputs are independent instances so we re-use one shared list — the
    test inspects `client.calls` directly after the send returns.
    """
    client = _RecordingClient()

    def _factory(*a: Any, **kw: Any) -> _RecordingClient:
        return client

    monkeypatch.setattr("httpx.AsyncClient", _factory)
    return client


# ─── Discord ─────────────────────────────────────────────────────────────

async def test_discord_skips_when_disabled(capture):
    out = DiscordOutput(DiscordOutputConfig(enabled=False, webhook_url="x"))
    await out.send(_analyzed())
    assert capture.calls == []


async def test_discord_skips_when_url_missing(capture):
    out = DiscordOutput(DiscordOutputConfig(enabled=True, webhook_url=""))
    await out.send(_analyzed())
    assert capture.calls == []


async def test_discord_respects_min_severity(capture):
    out = DiscordOutput(
        DiscordOutputConfig(enabled=True, webhook_url="http://x", min_severity="critical")
    )
    await out.send(_analyzed(severity=Severity.HIGH))
    assert capture.calls == []


async def test_discord_sends_embed_with_color_and_fields(capture):
    out = DiscordOutput(
        DiscordOutputConfig(
            enabled=True,
            webhook_url="http://discord/api/webhooks/x/y",
            username="HomelabSage",
            min_severity="info",
        )
    )
    await out.send(
        _analyzed(
            severity=Severity.CRITICAL,
            breaking=["env: OLD_X → NEW_X", "data dir moved to /data"],
        )
    )
    assert len(capture.calls) == 1
    body = capture.calls[0]["json"]
    assert body["username"] == "HomelabSage"
    assert len(body["embeds"]) == 1
    embed = body["embeds"][0]
    assert embed["color"] == 0xC0392B  # critical = red
    assert "openclaw" in embed["title"]
    assert embed["url"] == "https://example.com/r/5.7.0"
    field_names = [f["name"] for f in embed["fields"]]
    assert any("summary" in n.lower() for n in field_names)
    assert any("Breaking" in n for n in field_names)
    assert any("Recommended" in n for n in field_names)


async def test_discord_skips_when_no_analysis(capture):
    item = _analyzed()
    item.analysis = None
    out = DiscordOutput(DiscordOutputConfig(enabled=True, webhook_url="http://x"))
    await out.send(item)
    assert capture.calls == []


# ─── Ntfy ────────────────────────────────────────────────────────────────

async def test_ntfy_skips_when_topic_missing(capture):
    out = NtfyOutput(NtfyOutputConfig(enabled=True, server_url="https://ntfy.sh", topic=""))
    await out.send(_analyzed())
    assert capture.calls == []


async def test_ntfy_builds_topic_url_and_headers(capture):
    out = NtfyOutput(
        NtfyOutputConfig(
            enabled=True,
            server_url="https://ntfy.example.com",
            topic="homelab-alerts",
            auth_token="tk_secret",
            min_severity="info",
        )
    )
    await out.send(
        _analyzed(severity=Severity.CRITICAL, breaking=["env: A → B"])
    )
    assert len(capture.calls) == 1
    call = capture.calls[0]
    assert call["url"] == "https://ntfy.example.com/homelab-alerts"
    headers = call["headers"]
    assert headers["Authorization"] == "Bearer tk_secret"
    assert headers["Priority"] == "5"  # critical
    assert "rotating_light" in headers["Tags"]
    assert "openclaw" in headers["Title"]
    assert headers["Click"] == "https://example.com/r/5.7.0"
    body = call["content"].decode("utf-8")
    assert "Breaking" in body
    assert "env: A → B" in body


async def test_ntfy_omits_auth_header_when_token_blank(capture):
    out = NtfyOutput(
        NtfyOutputConfig(
            enabled=True, server_url="https://ntfy.sh", topic="x", auth_token=""
        )
    )
    await out.send(_analyzed(severity=Severity.HIGH))
    assert "Authorization" not in capture.calls[0]["headers"]


async def test_ntfy_handles_trailing_slash_in_server_url(capture):
    out = NtfyOutput(
        NtfyOutputConfig(enabled=True, server_url="https://ntfy.sh/", topic="x")
    )
    await out.send(_analyzed(severity=Severity.HIGH))
    assert capture.calls[0]["url"] == "https://ntfy.sh/x"


# ─── Gotify ──────────────────────────────────────────────────────────────

async def test_gotify_skips_when_token_missing(capture):
    out = GotifyOutput(
        GotifyOutputConfig(enabled=True, server_url="http://gotify", token="")
    )
    await out.send(_analyzed())
    assert capture.calls == []


async def test_gotify_posts_message_with_priority(capture):
    out = GotifyOutput(
        GotifyOutputConfig(
            enabled=True,
            server_url="https://gotify.example.com",
            token="AppToken123",
            min_severity="info",
        )
    )
    await out.send(_analyzed(severity=Severity.HIGH, breaking=["broke A", "broke B"]))
    assert len(capture.calls) == 1
    call = capture.calls[0]
    assert call["url"] == "https://gotify.example.com/message"
    assert call["params"] == {"token": "AppToken123"}
    body = call["json"]
    assert body["priority"] == 6  # high
    assert "openclaw" in body["title"]
    assert "Breaking" in body["message"]
    assert "broke A" in body["message"]


async def test_gotify_priority_overrides_take_precedence(capture):
    out = GotifyOutput(
        GotifyOutputConfig(
            enabled=True,
            server_url="http://gotify",
            token="x",
            min_severity="info",
            priority_overrides={"critical": 10, "info": 1},
        )
    )
    await out.send(_analyzed(severity=Severity.CRITICAL))
    assert capture.calls[0]["json"]["priority"] == 10
    await out.send(_analyzed(severity=Severity.INFO))
    assert capture.calls[1]["json"]["priority"] == 1


async def test_gotify_silent_swallows_http_errors(monkeypatch):
    class _FailingClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def post(self, *a, **kw):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: _FailingClient())
    out = GotifyOutput(
        GotifyOutputConfig(enabled=True, server_url="http://x", token="t")
    )
    # Must not raise — outputs are best-effort by contract.
    await out.send(_analyzed(severity=Severity.HIGH))
