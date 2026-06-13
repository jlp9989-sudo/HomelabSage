"""Guided-decoding (json_schema) mode for the analyzer.

Opt-in via `LLMConfig.json_schema`. When on, the analyzer sends the exact
`Analysis` schema for grammar-constrained decoding instead of free-form
`json_object` / `format:"json"`. Backends that reject schema-mode
`response_format` must degrade gracefully: one warning, retry in plain-JSON
mode, and remember not to try schema again for that model.

`httpx.AsyncClient.post` is monkeypatched directly (same approach as
test_outputs_webhook) so we can inspect the wire payload and script HTTP
failures per call.
"""

from __future__ import annotations

from typing import Any

import httpx

from homelabsage.config import LLMConfig
from homelabsage.llm import LLMClient, _analysis_json_schema
from homelabsage.models import Analysis, Severity, Update

_UPDATE = Update(
    source="docker", subject="mealie", current_version="3.16.0", new_version="3.17.0",
)

_GOOD_JSON = '{"severity":"high","summary":"ok"}'


class _Resp:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = ""

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("POST", "http://x"),
                response=self,  # type: ignore[arg-type]
            )

    def json(self) -> dict:
        return self._body


class _ScriptedClient:
    """Records every POST and returns responses from a scripted status list.

    `statuses` is consumed one per call; once exhausted every further call
    is a 200. `body_for` builds the success body for whichever protocol the
    test exercises (OpenAI-compat vs Ollama have different shapes).
    """

    def __init__(self, statuses: list[int], body: dict):
        self.statuses = list(statuses)
        self.body = body
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> _ScriptedClient:
        return self

    async def __aexit__(self, *a: Any) -> None:
        pass

    async def post(self, url: str, **kw: Any) -> _Resp:
        self.calls.append({"url": url, **kw})
        status = self.statuses.pop(0) if self.statuses else 200
        return _Resp(status, self.body)


def _install(monkeypatch, client: _ScriptedClient) -> _ScriptedClient:
    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: client)
    return client


def _openai_body() -> dict:
    return {
        "choices": [{"message": {"content": _GOOD_JSON}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _ollama_body() -> dict:
    return {"response": _GOOD_JSON, "prompt_eval_count": 10, "eval_count": 5}


# ─── drift guard ───────────────────────────────────────────────────────────

def test_schema_property_set_matches_model():
    """The hand-built schema must list exactly the Analysis fields, so it
    can't silently drift from the pydantic model."""
    schema = _analysis_json_schema()
    assert set(schema["properties"]) == set(Analysis.model_fields)
    # Every field is required (OpenAI strict mode demands it).
    assert set(schema["required"]) == set(Analysis.model_fields)
    assert schema["additionalProperties"] is False
    # Severity enum mirrors the model.
    assert set(schema["properties"]["severity"]["enum"]) == {s.value for s in Severity}


# ─── OpenAI-compat wire payload ──────────────────────────────────────────────

async def test_openai_compat_sends_json_schema_when_enabled(monkeypatch):
    client = _install(monkeypatch, _ScriptedClient([200], _openai_body()))
    cfg = LLMConfig(provider="groq", endpoint="https://api.groq.com/openai",
                    api_key="x", model="m", json_schema=True)
    result = await LLMClient(cfg).analyze(_UPDATE)
    assert result is not None and result.severity is Severity.HIGH
    assert len(client.calls) == 1
    rf = client.calls[0]["json"]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["schema"]["properties"].keys() == _analysis_json_schema()["properties"].keys()
    assert rf["json_schema"]["strict"] is True


async def test_openai_compat_sends_json_object_when_schema_disabled(monkeypatch):
    client = _install(monkeypatch, _ScriptedClient([200], _openai_body()))
    cfg = LLMConfig(provider="groq", endpoint="https://api.groq.com/openai",
                    api_key="x", model="m")  # json_schema defaults False
    await LLMClient(cfg).analyze(_UPDATE)
    assert client.calls[0]["json"]["response_format"] == {"type": "json_object"}


# ─── Ollama wire payload ──────────────────────────────────────────────────

async def test_ollama_sends_schema_object_when_enabled(monkeypatch):
    client = _install(monkeypatch, _ScriptedClient([200], _ollama_body()))
    cfg = LLMConfig(provider="ollama", endpoint="http://localhost:11434",
                    model="qwen3:30b", json_schema=True)
    await LLMClient(cfg).analyze(_UPDATE)
    fmt = client.calls[0]["json"]["format"]
    assert isinstance(fmt, dict) and fmt["type"] == "object"


async def test_ollama_sends_format_json_when_schema_disabled(monkeypatch):
    client = _install(monkeypatch, _ScriptedClient([200], _ollama_body()))
    cfg = LLMConfig(provider="ollama", endpoint="http://localhost:11434", model="m")
    await LLMClient(cfg).analyze(_UPDATE)
    assert client.calls[0]["json"]["format"] == "json"


# ─── graceful fallback ──────────────────────────────────────────────────────

async def test_openai_compat_falls_back_to_json_object_on_400(monkeypatch):
    # First call (schema mode) → 400; the retry (json_object) → 200.
    client = _install(monkeypatch, _ScriptedClient([400, 200], _openai_body()))
    cfg = LLMConfig(provider="anthropic", endpoint="https://api.anthropic.com/v1",
                    api_key="x", model="claude-haiku-4-5", json_schema=True)
    result = await LLMClient(cfg).analyze(_UPDATE)
    assert result is not None and result.severity is Severity.HIGH
    assert len(client.calls) == 2
    assert client.calls[0]["json"]["response_format"]["type"] == "json_schema"
    assert client.calls[1]["json"]["response_format"] == {"type": "json_object"}


async def test_non_schema_4xx_is_not_swallowed(monkeypatch):
    # A 400 that is NOT a schema-mode probe must propagate (caller logs it as
    # a failed analyze and returns None) — we must not retry it as if it were
    # a schema rejection.
    client = _install(monkeypatch, _ScriptedClient([400, 200], _openai_body()))
    cfg = LLMConfig(provider="groq", endpoint="https://api.groq.com/openai",
                    api_key="x", model="m")  # schema OFF → first 400 is terminal
    result = await LLMClient(cfg).analyze(_UPDATE)
    assert result is None
    assert len(client.calls) == 1  # no retry


async def test_schema_rejection_is_remembered_per_model(monkeypatch):
    # After one model 400s on schema, the SAME client must skip schema mode on
    # the next call (one POST, straight to json_object) instead of probing
    # every time.
    client = _install(monkeypatch, _ScriptedClient([400], _openai_body()))
    cfg = LLMConfig(provider="anthropic", endpoint="https://api.anthropic.com/v1",
                    api_key="x", model="claude-haiku-4-5", json_schema=True)
    llm = LLMClient(cfg)
    await llm.analyze(_UPDATE)        # probes (400) then falls back → 2 calls
    assert len(client.calls) == 2
    client.calls.clear()
    client.statuses = [200]
    await llm.analyze(_UPDATE)        # remembered → straight to json_object
    assert len(client.calls) == 1
    assert client.calls[0]["json"]["response_format"] == {"type": "json_object"}


async def test_curator_text_path_never_sends_schema(monkeypatch):
    # generate_text() uses strict_json=False (curator wants Markdown). Even
    # with json_schema=True the schema must not be attached.
    client = _install(monkeypatch, _ScriptedClient([200],
                      {"choices": [{"message": {"content": "free text"}}]}))
    cfg = LLMConfig(provider="groq", endpoint="https://api.groq.com/openai",
                    api_key="x", model="m", json_schema=True)
    out = await LLMClient(cfg).generate_text("write a note")
    assert out == "free text"
    assert "response_format" not in client.calls[0]["json"]
