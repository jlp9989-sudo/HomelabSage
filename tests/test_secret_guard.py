"""Tests for the pre-LLM secret-leak guard."""

from __future__ import annotations

import asyncio

from homelabsage.config import LLMConfig
from homelabsage.llm import LLMClient
from homelabsage.models import Update
from homelabsage.secret_guard import (
    PLACEHOLDER,
    GuardReport,
    redact_context,
    redact_text,
    should_guard,
)

# ─── should_guard ────────────────────────────────────────────────────


def test_should_guard_defaults_on_for_cloud():
    assert should_guard("openai")
    assert should_guard("groq")
    assert should_guard("anthropic")
    assert should_guard("gemini")


def test_should_guard_defaults_off_for_local():
    assert not should_guard("ollama")
    assert not should_guard("disabled")


def test_should_guard_explicit_override_wins():
    assert should_guard("ollama", override=True)
    assert not should_guard("openai", override=False)


# ─── inline + line-block + kv-line redaction ────────────────────────


def test_redact_inline_github_pat():
    text = "Token is ghp_abcdefghijklmnopqrstuvwxyz1234567890 — keep secret"
    out, rep = redact_text(text)
    assert "ghp_abcdef" not in out
    assert PLACEHOLDER in out
    assert rep.redacted_count >= 1


def test_redact_inline_openai_key():
    text = "OPENAI=sk-abc1234567890DEFGHIJKLMNOPQRSTUVWXYZab and"
    out, _ = redact_text(text)
    assert "sk-abc" not in out


def test_redact_pem_block():
    pem = (
        "header text\n"
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "AAAAB3NzaC1lZDI1NTE5AAAAIPgKj/garbage\n"
        "more garbage line\n"
        "-----END OPENSSH PRIVATE KEY-----\n"
        "trailer\n"
    )
    out, rep = redact_text(pem)
    assert "BEGIN OPENSSH" not in out
    assert "header text" in out
    assert "trailer" in out
    assert rep.redacted_count >= 1
    assert "pem_block" in rep.matched_categories


def test_redact_kv_line():
    text = "DB_PASSWORD=hunter2-the-second\nport=5432"
    out, rep = redact_text(text)
    assert "hunter2" not in out
    assert "DB_PASSWORD=" in out
    assert "port=5432" in out
    assert rep.redacted_count >= 1


def test_redact_aws_access_key():
    text = "key AKIAIOSFODNN7EXAMPLE more"
    out, _ = redact_text(text)
    assert "AKIA" not in out


def test_redact_telegram_bot_token():
    text = "tg=123456789:ABCdefGhIJKlmNopqrstuvWXYz123456789-_ extra"
    out, _ = redact_text(text)
    assert "ABCdefGh" not in out


def test_redact_discord_webhook():
    # Token must be ≥50 chars of [A-Za-z0-9_-] for the regex to match.
    text = "see https://discord.com/api/webhooks/123456789012345678/" \
           "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_ABCDEFGHIJKL_more_chars_here before"
    out, _ = redact_text(text)
    assert "AbCdEfGh" not in out


def test_redact_text_empty_is_passthrough():
    out, rep = redact_text("")
    assert out == ""
    assert rep.redacted_count == 0


# ─── context walker ──────────────────────────────────────────────────


def test_redact_context_key_match():
    ctx = {"GITHUB_TOKEN": "ghp_abcdefghijklmnopqrstuvwxyz1234567890",
           "INNOCENT": "hello"}
    out, rep = redact_context(ctx)
    assert out["GITHUB_TOKEN"] == PLACEHOLDER
    assert out["INNOCENT"] == "hello"
    assert rep.redacted_count >= 1


def test_redact_context_nested_dict():
    ctx = {"outer": {"DATABASE_PASSWORD": "secret",
                     "ok": "value"}}
    out, _ = redact_context(ctx)
    assert out["outer"]["DATABASE_PASSWORD"] == PLACEHOLDER
    assert out["outer"]["ok"] == "value"


def test_redact_context_list_walked():
    ctx = {"envs": [{"API_KEY": "x"}, "ghs_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]}
    out, _ = redact_context(ctx)
    assert out["envs"][0]["API_KEY"] == PLACEHOLDER
    # The string value also gets the credential pattern redaction
    assert "ghs_" not in str(out["envs"][1])


def test_report_dedupes_categories():
    rep = GuardReport()
    rep.note("kv_line")
    rep.note("kv_line")
    rep.note("pem_block")
    assert rep.redacted_count == 3
    assert rep.matched_categories == ["kv_line", "pem_block"]


# ─── LLM integration: the prompt is sanitised before send ──────────


def test_llm_analyze_redacts_before_prompt(monkeypatch):
    """Build an LLMClient with a cloud provider + stub the transport;
    confirm the prompt the transport sees does NOT contain the secret."""
    cfg = LLMConfig(provider="openai", model="gpt-x", endpoint="http://stub",
                    api_key="dummy")
    client = LLMClient(cfg)
    seen_prompts: list[str] = []

    async def fake_call(prompt: str, *, strict_json: bool = True,
                        temperature: float = 0.2) -> tuple[str, int, int, bool]:
        seen_prompts.append(prompt)
        return '{"severity":"info","summary":"ok"}', 10, 5, True

    monkeypatch.setattr(client, "_call_with_usage", fake_call)
    upd = Update(
        source="docker", subject="x",
        current_version="1.0", new_version="2.0",
        release_notes="GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz1234567890",
        context={"API_KEY": "supersecret"},
    )
    asyncio.run(client.analyze(upd, notes=""))
    assert seen_prompts, "_call_with_usage should have been invoked"
    full_prompt = seen_prompts[0]
    assert "ghp_abcdef" not in full_prompt
    assert "supersecret" not in full_prompt
    assert PLACEHOLDER in full_prompt


def test_llm_analyze_does_not_redact_for_local(monkeypatch):
    """Local providers (ollama, disabled) keep full fidelity prompts."""
    cfg = LLMConfig(provider="ollama", model="qwen", endpoint="http://stub")
    client = LLMClient(cfg)
    seen_prompts: list[str] = []

    async def fake_call(prompt: str, *, strict_json: bool = True,
                        temperature: float = 0.2) -> tuple[str, int, int, bool]:
        seen_prompts.append(prompt)
        return '{"severity":"info","summary":"ok"}', 10, 5, True

    monkeypatch.setattr(client, "_call_with_usage", fake_call)
    upd = Update(
        source="docker", subject="x",
        current_version="1.0", new_version="2.0",
        release_notes="ghp_abcdefghijklmnopqrstuvwxyz1234567890",
        context={},
    )
    asyncio.run(client.analyze(upd, notes=""))
    # Local → not redacted
    assert "ghp_abcdef" in seen_prompts[0]


def test_llm_analyze_force_off_via_override(monkeypatch):
    cfg = LLMConfig(provider="openai", model="x", endpoint="http://s",
                    api_key="d", secret_guard=False)
    client = LLMClient(cfg)
    seen_prompts: list[str] = []

    async def fake_call(prompt: str, *, strict_json: bool = True,
                        temperature: float = 0.2):
        seen_prompts.append(prompt)
        return '{"severity":"info","summary":"ok"}', 10, 5, True

    monkeypatch.setattr(client, "_call_with_usage", fake_call)
    upd = Update(
        source="docker", subject="x",
        current_version="1", new_version="2",
        release_notes="ghp_abcdefghijklmnopqrstuvwxyz1234567890",
    )
    asyncio.run(client.analyze(upd))
    assert "ghp_abcdef" in seen_prompts[0]


def test_redact_context_keeps_non_secret_strings():
    ctx = {"image": "linuxserver/plex:1.32", "ports": [32400, 32400]}
    out, _ = redact_context(ctx)
    assert out["image"] == "linuxserver/plex:1.32"
    assert out["ports"] == [32400, 32400]
