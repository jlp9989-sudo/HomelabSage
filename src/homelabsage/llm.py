"""LLM client — Ollama-compatible (works with Ollama, llama.cpp server, LM Studio…).

Sends a structured prompt asking for JSON output. Falls back gracefully if the
LLM is unreachable or returns malformed JSON.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from pydantic import ValidationError

from .config import LLMConfig
from .models import Analysis, Severity, Update
from .prompts import load_template as _load_prompt_template


@dataclass
class LastCall:
    """Auditable record of the most recent `LLMClient` call.

    Read by the Engine after every analyze() to:
      - persist the explainer row (prompt + raw_response + notes used)
      - record usage stats (tokens_in/out, duration, succeeded)

    Reset to None at the start of each call so a failed HTTP request
    doesn't leak the previous call's content into the next explainer.
    """

    prompt: str
    raw_response: str
    tokens_in: int
    tokens_out: int
    tokens_estimated: bool
    duration_ms: int
    succeeded: bool

log = logging.getLogger(__name__)


# Reasoning models (Deepseek-R1, Qwen3 *-Think, Groq's qwen/qwen3-32b, …) sometimes
# inline their chain-of-thought as a `<think>...</think>` block in the response
# `content` field instead of using an out-of-band `reasoning_content`. Without
# stripping, the entire trace lands in the curator's note or breaks the JSON parser.
# Be liberal about whitespace and accept malformed close tags defensively.
_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE)
# Defensive: if a model opens <think> but never closes (truncated stream), drop
# everything up to the first sane downstream content marker so we don't keep
# pages of reasoning. We only fire this when the unclosed <think> sits at the
# very beginning of the response, to avoid eating real output.
_UNCLOSED_THINK_PREFIX_RE = re.compile(
    r"\A\s*<think\b[^>]*>.*?(?=\n\s*(?:[{\[#\-*]|[A-Z][a-z]))",
    re.DOTALL | re.IGNORECASE,
)


def _strip_think_blocks(text: str) -> str:
    """Remove `<think>...</think>` reasoning blocks from an LLM response.

    Behaviour:
      - Removes every well-formed `<think>...</think>` pair, case-insensitive,
        attributes tolerated (`<think type="reasoning">`).
      - If the response opens with an *unclosed* `<think>` tag (truncated
        stream), drop the prefix up to the next plausible content start.
      - Trims surrounding whitespace introduced by the removal.
      - Returns the input unchanged when no tag is present.
    """
    if not text or "<think" not in text.lower():
        return text
    stripped = _THINK_BLOCK_RE.sub("", text)
    if "<think" in stripped.lower():
        # Still has an opener — must be unclosed. Try the defensive prefix cut.
        stripped = _UNCLOSED_THINK_PREFIX_RE.sub("", stripped)
    return stripped.strip()


# Prompt body lives under `homelabsage/prompts/analyzer.md` so iterating on
# the rules doesn't require touching Python. Imported at module top, loaded
# lazily + cached on first build_prompt() call (see prompts/__init__.py).


def build_prompt(update: Update, notes: str = "") -> str:
    return _load_prompt_template("analyzer").format(
        source=update.source,
        subject=update.subject,
        current_version=update.current_version,
        new_version=update.new_version,
        release_url=update.release_url or "(none)",
        context=json.dumps(update.context, indent=2, default=str) if update.context else "(none)",
        notes=notes.strip() or "(no relevant notes)",
        release_notes=(update.release_notes or "(no release notes)").strip()[:15000],
    )


# Provider presets — sensible defaults for `endpoint` and `model` per
# provider so the settings UI can auto-fill them when the user picks a
# provider from the dropdown. The keys MUST match LLMConfig.provider's
# Literal values; the renderer relies on that for the JS lookup.
#
# Conventions:
#   - `endpoint` is the *base URL the user would paste*, not the fully
#     qualified chat-completions URL — `_resolve_chat_completions_url`
#     normalizes it at call time.
#   - `model` is a known-stable, low-cost default per provider. The JS
#     overwrites the model field whenever the user picks a new provider
#     from the dropdown; if they want a different model on the same
#     provider they just type it AFTER selecting the provider.
#   - `protocol` is the wire format. Anything that's not `ollama` or
#     `disabled` speaks the OpenAI chat-completions shape; we still tag
#     each one explicitly so the dispatch isn't a moving target as we
#     add providers.
PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "ollama": {
        "endpoint": "http://localhost:11434",
        "model": "qwen3:30b",
        "protocol": "ollama",
    },
    "openai": {
        "endpoint": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "protocol": "openai_compat",
    },
    "groq": {
        "endpoint": "https://api.groq.com/openai",
        "model": "llama-3.3-70b-versatile",
        "protocol": "openai_compat",
    },
    "gemini": {
        "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.5-flash",
        "protocol": "openai_compat",
    },
    "openrouter": {
        "endpoint": "https://openrouter.ai/api/v1",
        "model": "anthropic/claude-3.5-haiku",
        "protocol": "openai_compat",
    },
    "anthropic": {
        "endpoint": "https://api.anthropic.com/v1",
        "model": "claude-haiku-4-5",
        "protocol": "openai_compat",
    },
    "disabled": {
        "endpoint": "",
        "model": "",
        "protocol": "disabled",
    },
}


def _resolve_chat_completions_url(endpoint: str) -> str:
    """Build the full chat-completions URL from a user-supplied endpoint.

    Endpoint convention varies across providers:
      - OpenAI / Groq / OpenRouter base URL → append /v1/chat/completions
      - Gemini's /v1beta/openai             → append only /chat/completions
      - User pasted full /chat/completions  → use as-is
    """
    e = endpoint.rstrip("/")
    if e.endswith("/chat/completions"):
        return e
    if "/v1" in e.lower() or "/v2" in e.lower():
        return e + "/chat/completions"
    return e + "/v1/chat/completions"


class LLMClient:
    """Thin wrapper around the chat-completions HTTP call.

    `cfg_provider` can be either a fixed `LLMConfig` (the simple case used
    by tests and one-shot scripts) or a callable that returns one on every
    access (used by the engine so changing `llm_active` or editing the
    overlay takes effect on the next call without restarting the process).
    """

    def __init__(self, cfg_provider: LLMConfig | Callable[[], LLMConfig]):
        if callable(cfg_provider):
            self._provider = cfg_provider
        else:
            fixed = cfg_provider
            self._provider = lambda: fixed
        # Most-recent call record. The Engine reads this after analyze()
        # to persist an explainer row (prompt+response) AND a usage row
        # (tokens, duration, succeeded). Reset to None at the start of
        # each call so a failed HTTP request doesn't leak the previous
        # call's content into the next audit row.
        self.last_call: LastCall | None = None

    @property
    def cfg(self) -> LLMConfig:
        return self._provider()

    def is_enabled(self) -> bool:
        return self.cfg.provider != "disabled"

    async def analyze(self, update: Update, notes: str = "") -> Analysis | None:
        self.last_call = None
        if not self.is_enabled():
            return None
        prompt = build_prompt(update, notes=notes)
        started = time.monotonic()
        try:
            raw, tin, tout, est = await self._call_with_usage(
                prompt, strict_json=self.cfg.strict_json,
            )
        except Exception as e:
            log.warning("LLM call failed for %s: %s", update.subject, e)
            # Record the failure for usage stats — the engine sees
            # last_call.succeeded=False and persists a row anyway so
            # quota dashboards reflect failed-call attempts too.
            self.last_call = LastCall(
                prompt=prompt, raw_response="",
                tokens_in=_estimate_tokens(prompt), tokens_out=0,
                tokens_estimated=True,
                duration_ms=int((time.monotonic() - started) * 1000),
                succeeded=False,
            )
            return None
        # Capture for the explainer table BEFORE parsing — even if parse
        # fails we want the audit record to show what the model said.
        self.last_call = LastCall(
            prompt=prompt, raw_response=raw,
            tokens_in=tin, tokens_out=tout, tokens_estimated=est,
            duration_ms=int((time.monotonic() - started) * 1000),
            succeeded=True,
        )
        return _parse_analysis(raw)

    async def generate_text(self, prompt: str, *, temperature: float = 0.0) -> str | None:
        """Free-form text completion (no JSON schema enforcement).

        Used by the curator, which expects Markdown back, not a JSON object.
        Temperature defaults to 0.0 — the curator is a factual task and any
        creativity quickly turns into invented "facts".
        Returns None if the LLM is disabled or the call fails.
        """
        if not self.is_enabled():
            return None
        try:
            return await self._call(prompt, strict_json=False, temperature=temperature)
        except Exception as e:
            log.warning("LLM generate_text failed: %s", e)
            return None

    async def _call(
        self, prompt: str, strict_json: bool, temperature: float = 0.2
    ) -> str:
        """Backwards-compat wrapper around `_call_with_usage` for callers
        that only want the text. Use `_call_with_usage` directly when you
        need token counts (e.g. the analyzer's LastCall build)."""
        text, _tin, _tout, _estimated = await self._call_with_usage(
            prompt, strict_json=strict_json, temperature=temperature,
        )
        return text

    async def _call_with_usage(
        self, prompt: str, strict_json: bool, temperature: float = 0.2,
    ) -> tuple[str, int, int, bool]:
        """Returns `(text, tokens_in, tokens_out, estimated)`.

        `estimated=True` means the provider didn't report usage and we
        applied a 4-chars-per-token heuristic so totals stay populated.
        """
        # Dispatch by the provider's protocol so adding a new openai-compat
        # provider (next Groq clone, next Gemini revision, …) doesn't need a
        # branch here — only a PROVIDER_PRESETS entry.
        protocol = PROVIDER_PRESETS.get(self.cfg.provider, {}).get("protocol")
        if protocol == "ollama":
            text, tin, tout, est = await self._call_ollama(
                prompt, strict_json=strict_json, temperature=temperature,
            )
        elif protocol == "openai_compat":
            text, tin, tout, est = await self._call_openai_compat(
                prompt, strict_json=strict_json, temperature=temperature,
            )
        else:
            raise ValueError(f"unknown LLM provider: {self.cfg.provider}")
        return _strip_think_blocks(text), tin, tout, est

    async def _call_ollama(
        self, prompt: str, strict_json: bool, temperature: float,
    ) -> tuple[str, int, int, bool]:
        """Ollama-compat: POST /api/generate, format=json forces JSON output.

        Ollama reports usage as `prompt_eval_count` + `eval_count`. When
        absent (very old servers, custom forks) we fall back to estimates.
        """
        url = self.cfg.endpoint.rstrip("/") + "/api/generate"
        payload = {
            "model": self.cfg.model,
            "prompt": prompt,
            "stream": False,
            "format": "json" if strict_json else None,
            "options": {"num_ctx": self.cfg.context_size, "temperature": temperature},
        }
        # Drop nullable to avoid backend confusion
        payload = {k: v for k, v in payload.items() if v is not None}
        async with httpx.AsyncClient(timeout=self.cfg.timeout) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
            text = data.get("response", "")
            tin = int(data.get("prompt_eval_count") or 0)
            tout = int(data.get("eval_count") or 0)
            estimated = False
            if tin == 0:
                tin = _estimate_tokens(prompt)
                estimated = True
            if tout == 0:
                tout = _estimate_tokens(text)
                estimated = True
            return text, tin, tout, estimated

    async def _call_openai_compat(
        self, prompt: str, strict_json: bool, temperature: float,
    ) -> tuple[str, int, int, bool]:
        """OpenAI-compatible chat completions.

        `usage.prompt_tokens` + `usage.completion_tokens` per the OpenAI
        spec; Groq/Gemini/OpenRouter all return them.
        """
        url = _resolve_chat_completions_url(self.cfg.endpoint)
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"} if self.cfg.api_key else {}
        payload = {
            "model": self.cfg.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "response_format": {"type": "json_object"} if strict_json else None,
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        async with httpx.AsyncClient(timeout=self.cfg.timeout) as client:
            r = await client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage") or {}
            tin = int(usage.get("prompt_tokens") or 0)
            tout = int(usage.get("completion_tokens") or 0)
            estimated = False
            if tin == 0:
                tin = _estimate_tokens(prompt)
                estimated = True
            if tout == 0:
                tout = _estimate_tokens(text)
                estimated = True
            return text, tin, tout, estimated


def _estimate_tokens(text: str) -> int:
    """Crude chars / 4 estimate when the provider didn't report usage."""
    return max(1, len(text) // 4)


def _parse_analysis(raw: str) -> Analysis | None:
    """Tolerant JSON parser — handles models that wrap JSON in markdown fences."""
    text = raw.strip()
    # Strip ```json ... ``` if present
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip("\n")
        if text.endswith("```"):
            text = text[:-3]
    # Find first { and last } if there's surrounding noise
    if not text.startswith("{"):
        i = text.find("{")
        j = text.rfind("}")
        if i >= 0 and j > i:
            text = text[i : j + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        log.warning("LLM returned non-JSON: %s", e)
        return None
    # Normalize severity case
    if "severity" in data and isinstance(data["severity"], str):
        data["severity"] = data["severity"].lower()
    try:
        return Analysis(**data)
    except ValidationError as e:
        log.warning("LLM JSON did not match schema: %s", e)
        # Best-effort fallback: keep summary if present
        if "summary" in data:
            return Analysis(severity=Severity.INFO, summary=str(data["summary"])[:500])
        return None
