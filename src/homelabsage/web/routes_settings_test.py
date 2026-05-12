"""Connection-test endpoints for settings blocks with external integrations.

Each `POST /settings/.../test` reads the currently SAVED config (overlay +
defaults), makes one minimal API call to the external service, and returns
an HTML fragment (success or error) intended for an HTMX swap into the
`#test-result` div inside the settings form.

Why test against the saved config rather than the form contents? Two reasons:
the form values are unvalidated (they may still be in-progress edits the user
hasn't typed fully), and a "Test" that's coupled to form state has to
duplicate the form-to-config coercion logic. Saving is fast, validated, and
reversible — the user pattern is "type → save → test".

Each helper returns `(ok, message)`. The route layer wraps both into the
shared `_test_result.html` fragment. Helpers are isolated module-level
functions so tests can monkeypatch them cleanly.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from jinja2 import Environment

from ..config import (
    Config,
    HAConfig,
    LLMConfig,
    NotionOutputConfig,
    SchedulerConfig,
    TelegramOutputConfig,
    get_active_llm_config,
    load_config,
)
from ..llm import _resolve_chat_completions_url

log = logging.getLogger(__name__)

# Short timeout for every test call. Cold LLMs may take >15s for first-token,
# but a connection-test isn't a real generation — we only need the endpoint
# to return SOMETHING. If the model is so slow it can't reply to a one-word
# prompt in 30s, surfacing a timeout error is the correct result.
TEST_TIMEOUT_S = 30.0


# ─── helpers — pure, no FastAPI dependency, easy to monkeypatch ─────────


async def _test_llm(llm_cfg: LLMConfig) -> tuple[bool, str]:
    """Send a one-word prompt; success = HTTP 200 with parseable response shape.

    We don't check the *content* of the reply — different models behave
    differently for a ping, and the goal is "endpoint reachable + auth works",
    not "model is useful". Content quality is exercised by a real scan.
    """
    if llm_cfg.provider == "disabled":
        return False, "LLM provider is set to 'disabled' — switch to ollama / openai / anthropic first."
    if not llm_cfg.endpoint:
        return False, "No endpoint configured."

    if llm_cfg.provider == "ollama":
        url = llm_cfg.endpoint.rstrip("/") + "/api/generate"
        payload: dict = {"model": llm_cfg.model, "prompt": "ping", "stream": False}
        headers: dict = {}
    else:  # openai / anthropic (OpenAI-compatible)
        url = _resolve_chat_completions_url(llm_cfg.endpoint)
        payload = {
            "model": llm_cfg.model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 4,
        }
        headers = {"Authorization": f"Bearer {llm_cfg.api_key}"} if llm_cfg.api_key else {}

    try:
        async with httpx.AsyncClient(timeout=TEST_TIMEOUT_S) as client:
            r = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        return False, f"Could not reach {url}: {e}"

    if r.status_code == 200:
        return True, f"Reached {url} (HTTP 200). Model {llm_cfg.model!r} responded."
    if r.status_code in (401, 403):
        return False, f"HTTP {r.status_code} — check the API key for provider {llm_cfg.provider!r}."
    if r.status_code == 404:
        return False, (
            "HTTP 404 — the endpoint exists but doesn't expose the expected path. "
            "Verify the base URL: ollama wants the host root, openai-compatible "
            "wants the `/v1` base."
        )
    return False, f"HTTP {r.status_code}: {r.text[:200]}"


async def _test_notion(notion_cfg: NotionOutputConfig) -> tuple[bool, str]:
    """`GET /v1/users/me` validates the integration secret without writing.

    A 200 means the secret is valid; we also verify the DB id parses if set.
    A missing DB id is not a hard failure here — the field can be empty if
    the integration was just created, but the secret is what we're testing.
    """
    if not notion_cfg.api_key:
        return False, "No api_key set. Create an Internal Integration in Notion and paste the secret here."

    headers = {
        "Authorization": f"Bearer {notion_cfg.api_key}",
        "Notion-Version": "2022-06-28",
    }
    try:
        async with httpx.AsyncClient(timeout=TEST_TIMEOUT_S) as client:
            r = await client.get("https://api.notion.com/v1/users/me", headers=headers)
    except httpx.HTTPError as e:
        return False, f"Could not reach api.notion.com: {e}"

    if r.status_code == 200:
        bot = r.json().get("name") or r.json().get("bot", {}).get("owner", {}).get("user", {}).get("name") or "bot"
        msg = f"Integration secret accepted (authenticated as {bot!r})."
        if notion_cfg.database_id:
            # Also touch the DB so the user catches "DB not shared with integration" early.
            try:
                async with httpx.AsyncClient(timeout=TEST_TIMEOUT_S) as client:
                    rdb = await client.get(
                        f"https://api.notion.com/v1/databases/{notion_cfg.database_id}",
                        headers=headers,
                    )
                if rdb.status_code == 200:
                    title_parts = rdb.json().get("title") or []
                    db_name = "".join(p.get("plain_text", "") for p in title_parts) or notion_cfg.database_id
                    msg += f" Database {db_name!r} reachable."
                elif rdb.status_code == 404:
                    return False, (
                        f"{msg} BUT database {notion_cfg.database_id!r} is NOT shared "
                        f"with this integration — open it in Notion and add the integration "
                        f"under ⋯ → Connections."
                    )
                else:
                    return False, f"{msg} Database lookup failed: HTTP {rdb.status_code}."
            except httpx.HTTPError as e:
                return False, f"{msg} Database lookup failed: {e}"
        return True, msg
    if r.status_code == 401:
        return False, "HTTP 401 — the api_key is not valid (rotated? wrong workspace?)."
    return False, f"HTTP {r.status_code}: {r.text[:200]}"


async def _test_telegram(tg_cfg: TelegramOutputConfig) -> tuple[bool, str]:
    """`getMe` validates the bot token; then `sendMessage` proves chat_id is reachable.

    `sendMessage` is intentional: a wrong chat_id is the #1 silent failure
    (the bot works, the API accepts the call, but the user never gets the
    message). Sending a real "✅ test from HomelabSage" message catches this
    immediately.
    """
    if not tg_cfg.bot_token:
        return False, "No bot_token set. Talk to @BotFather and run /newbot."

    base = f"https://api.telegram.org/bot{tg_cfg.bot_token}"
    try:
        async with httpx.AsyncClient(timeout=TEST_TIMEOUT_S) as client:
            rme = await client.get(f"{base}/getMe")
    except httpx.HTTPError as e:
        return False, f"Could not reach api.telegram.org: {e}"

    if rme.status_code != 200:
        return False, f"getMe failed with HTTP {rme.status_code}: {rme.text[:200]} — the bot_token is probably wrong."

    bot_name = rme.json().get("result", {}).get("username", "(unknown)")
    msg = f"Bot @{bot_name} reachable."
    if not tg_cfg.chat_id:
        return True, f"{msg} No chat_id set, so no test message sent."

    try:
        async with httpx.AsyncClient(timeout=TEST_TIMEOUT_S) as client:
            rsend = await client.post(
                f"{base}/sendMessage",
                json={"chat_id": tg_cfg.chat_id, "text": "✅ HomelabSage test message"},
            )
    except httpx.HTTPError as e:
        return False, f"{msg} sendMessage failed: {e}"

    if rsend.status_code == 200:
        return True, f"{msg} Test message delivered to chat {tg_cfg.chat_id}."
    return False, (
        f"{msg} sendMessage HTTP {rsend.status_code}: {rsend.text[:200]} — the "
        f"chat_id is wrong, or the user has not /start-ed the bot."
    )


async def _test_homeassistant(ha_cfg: HAConfig) -> tuple[bool, str]:
    """`GET /api/` returns `{"message": "API running."}` on a working install."""
    if not ha_cfg.token:
        return False, "No token set. In Home Assistant: profile → Security → Long-lived access tokens → Create."
    if not ha_cfg.url:
        return False, "No url set."

    url = ha_cfg.url.rstrip("/") + "/api/"
    try:
        async with httpx.AsyncClient(timeout=TEST_TIMEOUT_S) as client:
            r = await client.get(url, headers={"Authorization": f"Bearer {ha_cfg.token}"})
    except httpx.HTTPError as e:
        return False, f"Could not reach {url}: {e}"

    if r.status_code == 200:
        # /api/ replies with a literal message; if the user pointed us at
        # something else (e.g. the LAN dashboard root) this won't match.
        body = r.text.strip()
        if "API running" in body or '"message"' in body:
            return True, f"Home Assistant API reachable at {ha_cfg.url}."
        return False, f"HTTP 200 but the response doesn't look like HA's /api/: {body[:120]!r}"
    if r.status_code == 401:
        return False, "HTTP 401 — the long-lived access token is wrong or revoked."
    return False, f"HTTP {r.status_code}: {r.text[:200]}"


async def _test_heartbeat(scheduler_cfg: SchedulerConfig) -> tuple[bool, str]:
    """GET the heartbeat URL — success = any 2xx. Uptime Kuma push URLs
    return JSON `{"ok": true}`; opaque endpoints return whatever they want."""
    if not scheduler_cfg.heartbeat_url:
        return False, "No heartbeat_url set — leave empty to disable the heartbeat entirely."

    url = scheduler_cfg.heartbeat_url
    try:
        async with httpx.AsyncClient(timeout=TEST_TIMEOUT_S) as client:
            r = await client.get(url)
    except httpx.HTTPError as e:
        return False, f"Could not reach {url}: {e}"

    if 200 <= r.status_code < 300:
        return True, f"Heartbeat URL responded HTTP {r.status_code}. Response: {r.text[:120] or '(empty)'}"
    return False, f"HTTP {r.status_code}: {r.text[:200]}"


# ─── route registration ────────────────────────────────────────────────


def register_settings_test_routes(
    app: FastAPI, _cfg: Config, cfg_path: Path | None, env: Environment
) -> None:
    """Attach the five `POST /settings/.../test` endpoints to the app.

    `cfg_path` is used to re-load the saved config on each request so the
    test always reflects the most recent save (the user's flow is "edit →
    save → test", and we must NOT carry over stale in-memory cfg).
    """

    def _fresh_cfg() -> Config:
        return load_config(cfg_path) if cfg_path and cfg_path.exists() else _cfg

    def _render(ok: bool, message: str) -> HTMLResponse:
        tmpl = env.get_template("_settings_test_result.html")
        return HTMLResponse(tmpl.render(ok=ok, message=message))

    @app.post("/settings/llm/test", response_class=HTMLResponse)
    async def test_llm() -> HTMLResponse:
        ok, msg = await _test_llm(get_active_llm_config(_fresh_cfg()))
        return _render(ok, msg)

    @app.post("/settings/outputs/notion/test", response_class=HTMLResponse)
    async def test_notion() -> HTMLResponse:
        ok, msg = await _test_notion(_fresh_cfg().outputs.notion)
        return _render(ok, msg)

    @app.post("/settings/outputs/telegram/test", response_class=HTMLResponse)
    async def test_telegram() -> HTMLResponse:
        ok, msg = await _test_telegram(_fresh_cfg().outputs.telegram)
        return _render(ok, msg)

    @app.post("/settings/sources/homeassistant/test", response_class=HTMLResponse)
    async def test_homeassistant() -> HTMLResponse:
        ok, msg = await _test_homeassistant(_fresh_cfg().sources.homeassistant)
        return _render(ok, msg)

    @app.post("/settings/scheduler/heartbeat/test", response_class=HTMLResponse)
    async def test_heartbeat() -> HTMLResponse:
        ok, msg = await _test_heartbeat(_fresh_cfg().scheduler)
        return _render(ok, msg)


# Exported for the settings form template — maps a block name to its test endpoint
# URL so the renderer knows whether to show the "Test connection" button.
BLOCK_TEST_ENDPOINTS: dict[str, str] = {
    "llm": "/settings/llm/test",
    "outputs/notion": "/settings/outputs/notion/test",
    "outputs/telegram": "/settings/outputs/telegram/test",
    "sources/homeassistant": "/settings/sources/homeassistant/test",
    "scheduler": "/settings/scheduler/heartbeat/test",
}
