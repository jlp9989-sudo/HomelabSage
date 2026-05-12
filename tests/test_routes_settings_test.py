"""Tests for the connection-test endpoints under /settings/.../test.

Each external service has a small `_test_xxx(cfg) -> (ok, message)` helper.
We exercise:
  - The helpers themselves against an `httpx.MockTransport` so they catch
    real status-code branches (not just happy path) without hitting the
    public internet.
  - The route wiring: hitting the URL via the FastAPI TestClient returns
    the rendered fragment with the correct CSS class.

`httpx.AsyncClient` is patched at the module level so a mock transport
intercepts every call the helpers make.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from homelabsage import web
from homelabsage.config import (
    HAConfig,
    LLMConfig,
    NotionOutputConfig,
    SchedulerConfig,
    TelegramOutputConfig,
    load_config,
)
from homelabsage.web import routes_settings_test as rst


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "llm:\n"
        "  provider: openai\n"
        "  model: ping-model\n"
        "  endpoint: http://halo:11434/v1\n"
        "  api_key: sk-test\n"
        "  timeout: 5\n"
        "outputs:\n"
        "  notion:\n"
        "    enabled: true\n"
        "    api_key: ntn_secret\n"
        "    database_id: dbid12345\n"
        "  telegram:\n"
        "    enabled: true\n"
        "    bot_token: 123:ABC\n"
        "    chat_id: '42'\n"
        "sources:\n"
        "  homeassistant:\n"
        "    enabled: true\n"
        "    url: http://ha.lan:8123\n"
        "    token: ha-llt\n"
        "scheduler:\n"
        "  heartbeat_url: http://kuma.lan/api/push/abc\n"
        "storage:\n"
        f"  database_path: {tmp_path}/state.sqlite\n"
        "notes:\n"
        f"  notes_dir: {tmp_path}/notes\n"
        "web:\n"
        "  auth:\n"
        "    enabled: false\n"
    )
    return tmp_path


@pytest.fixture
def client(cfg_dir: Path) -> TestClient:
    cfg_path = cfg_dir / "config.yaml"
    cfg = load_config(cfg_path)
    app = web.create_app(cfg, cfg_path=cfg_path)
    return TestClient(app)


# ─── helpers — direct unit tests using httpx.MockTransport ────────────


def _patch_transport(monkeypatch, handler):
    """Make every `httpx.AsyncClient(...)` call inside `rst` use this handler."""
    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("homelabsage.web.routes_settings_test.httpx.AsyncClient", fake_client)


async def test_llm_helper_success(monkeypatch):
    def handler(req):
        assert req.url.path.endswith("/chat/completions")
        assert req.headers.get("authorization") == "Bearer sk-test"
        return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})
    _patch_transport(monkeypatch, handler)
    cfg = LLMConfig(provider="openai", model="ping-model", endpoint="http://halo:11434/v1", api_key="sk-test")
    ok, msg = await rst._test_llm(cfg)
    assert ok is True
    assert "ping-model" in msg


async def test_llm_helper_401_says_check_api_key(monkeypatch):
    _patch_transport(monkeypatch, lambda req: httpx.Response(401, text="bad auth"))
    cfg = LLMConfig(provider="openai", model="m", endpoint="http://halo:11434/v1", api_key="wrong")
    ok, msg = await rst._test_llm(cfg)
    assert ok is False
    assert "API key" in msg


async def test_llm_helper_provider_disabled():
    cfg = LLMConfig(provider="disabled")
    ok, msg = await rst._test_llm(cfg)
    assert ok is False
    assert "disabled" in msg.lower()


async def test_notion_helper_success_with_database(monkeypatch):
    def handler(req):
        if "/users/me" in str(req.url):
            return httpx.Response(200, json={"name": "homelabsage-bot"})
        if "/databases/" in str(req.url):
            return httpx.Response(200, json={"title": [{"plain_text": "Updates"}]})
        return httpx.Response(404)
    _patch_transport(monkeypatch, handler)
    cfg = NotionOutputConfig(enabled=True, api_key="ntn_xxx", database_id="abc")
    ok, msg = await rst._test_notion(cfg)
    assert ok is True
    assert "homelabsage-bot" in msg
    assert "Updates" in msg


async def test_notion_helper_db_not_shared(monkeypatch):
    def handler(req):
        if "/users/me" in str(req.url):
            return httpx.Response(200, json={"name": "bot"})
        return httpx.Response(404, json={"object": "error", "code": "object_not_found"})
    _patch_transport(monkeypatch, handler)
    cfg = NotionOutputConfig(enabled=True, api_key="ntn_xxx", database_id="abc")
    ok, msg = await rst._test_notion(cfg)
    assert ok is False
    assert "not shared" in msg.lower()


async def test_notion_helper_missing_key():
    ok, msg = await rst._test_notion(NotionOutputConfig())
    assert ok is False
    assert "api_key" in msg


async def test_telegram_helper_send_succeeds(monkeypatch):
    def handler(req):
        if req.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"result": {"username": "my_bot"}})
        if req.url.path.endswith("/sendMessage"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)
    _patch_transport(monkeypatch, handler)
    cfg = TelegramOutputConfig(enabled=True, bot_token="123:ABC", chat_id="42")
    ok, msg = await rst._test_telegram(cfg)
    assert ok is True
    assert "my_bot" in msg
    assert "42" in msg


async def test_telegram_helper_no_chat_id_skips_send(monkeypatch):
    def handler(req):
        assert "/sendMessage" not in req.url.path
        return httpx.Response(200, json={"result": {"username": "my_bot"}})
    _patch_transport(monkeypatch, handler)
    cfg = TelegramOutputConfig(enabled=True, bot_token="123:ABC", chat_id="")
    ok, msg = await rst._test_telegram(cfg)
    assert ok is True
    assert "no test message" in msg.lower() or "no chat_id" in msg.lower()


async def test_ha_helper_success(monkeypatch):
    _patch_transport(
        monkeypatch,
        lambda req: httpx.Response(200, json={"message": "API running."}),
    )
    cfg = HAConfig(enabled=True, url="http://ha.lan:8123", token="llt")
    ok, msg = await rst._test_homeassistant(cfg)
    assert ok is True
    assert "ha.lan" in msg


async def test_ha_helper_401(monkeypatch):
    _patch_transport(monkeypatch, lambda req: httpx.Response(401, text="bad token"))
    cfg = HAConfig(enabled=True, url="http://ha.lan:8123", token="bad")
    ok, msg = await rst._test_homeassistant(cfg)
    assert ok is False
    assert "401" in msg


async def test_heartbeat_helper_success(monkeypatch):
    _patch_transport(monkeypatch, lambda req: httpx.Response(200, text='{"ok":true}'))
    cfg = SchedulerConfig(heartbeat_url="http://kuma.lan/api/push/x")
    ok, msg = await rst._test_heartbeat(cfg)
    assert ok is True
    assert "200" in msg


async def test_heartbeat_helper_no_url():
    ok, msg = await rst._test_heartbeat(SchedulerConfig(heartbeat_url=""))
    assert ok is False


# ─── route wiring — does the form render the Test button and does POSTing work ─


def test_form_renders_test_button_for_llm(client):
    r = client.get("/settings/llm")
    assert "Test connection" in r.text
    assert 'hx-post="/settings/llm/test"' in r.text


def test_form_renders_test_button_for_notion(client):
    r = client.get("/settings/outputs/notion")
    assert 'hx-post="/settings/outputs/notion/test"' in r.text


def test_form_renders_test_button_for_telegram(client):
    r = client.get("/settings/outputs/telegram")
    assert 'hx-post="/settings/outputs/telegram/test"' in r.text


def test_form_renders_test_button_for_homeassistant(client):
    r = client.get("/settings/sources/homeassistant")
    assert 'hx-post="/settings/sources/homeassistant/test"' in r.text


def test_form_renders_test_button_for_scheduler_heartbeat(client):
    r = client.get("/settings/scheduler")
    assert 'hx-post="/settings/scheduler/heartbeat/test"' in r.text


def test_form_omits_test_button_for_blocks_without_external_service(client):
    r = client.get("/settings/storage")
    assert "Test connection" not in r.text


def test_test_endpoint_returns_html_fragment_on_success(client, monkeypatch):
    async def fake(_cfg):
        return True, "endpoint ok"
    monkeypatch.setattr("homelabsage.web.routes_settings_test._test_llm", fake)
    r = client.post("/settings/llm/test")
    assert r.status_code == 200
    assert 'id="test-result"' in r.text
    assert "flash success" in r.text
    assert "endpoint ok" in r.text


def test_test_endpoint_returns_error_fragment_on_failure(client, monkeypatch):
    async def fake(_cfg):
        return False, "boom"
    monkeypatch.setattr("homelabsage.web.routes_settings_test._test_notion", fake)
    r = client.post("/settings/outputs/notion/test")
    assert r.status_code == 200
    assert 'id="test-result"' in r.text
    assert "flash error" in r.text
    assert "boom" in r.text


def test_test_endpoint_reads_fresh_config(client, cfg_dir, monkeypatch):
    """The endpoint must reload config on every request so a recent Save is
    visible. Verify by mutating the overlay between two calls."""
    seen_keys: list[str] = []

    async def fake(cfg):
        seen_keys.append(cfg.api_key)
        return True, "ok"

    monkeypatch.setattr("homelabsage.web.routes_settings_test._test_notion", fake)
    client.post("/settings/outputs/notion/test")
    # Save a new api_key via the overlay write path
    from homelabsage.config_overlay import atomic_write_yaml, user_overlay_path
    atomic_write_yaml(
        user_overlay_path(cfg_dir / "config.yaml"),
        {"outputs": {"notion": {"api_key": "ntn_NEW"}}},
    )
    client.post("/settings/outputs/notion/test")
    assert seen_keys == ["ntn_secret", "ntn_NEW"]
