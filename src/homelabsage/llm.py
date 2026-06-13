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
from .prompts import assemble as _assemble_prompt
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


# Hard ceiling on the serialized context block. Individual enrichers are
# already bounded, but a single chatty one (docker logs, a sprawling env,
# bloated upstream release_notes nested in context) could still balloon the
# prompt without limit. This is a backstop against pathological payloads, not
# a tight token budget — it only trips well past the normal context size.
# release_notes is capped separately below ([:15000]).
_MAX_CONTEXT_CHARS = 32000


def _serialize_context(context: dict | None) -> str:
    if not context:
        return "(none)"
    dumped = json.dumps(context, indent=2, default=str)
    if len(dumped) <= _MAX_CONTEXT_CHARS:
        return dumped
    dropped = len(dumped) - _MAX_CONTEXT_CHARS
    return (
        dumped[:_MAX_CONTEXT_CHARS]
        + f"\n… [context truncated: {dropped} chars over {_MAX_CONTEXT_CHARS}-char cap]"
    )


def build_prompt(update: Update, notes: str = "") -> str:
    # Assemble first: keep only the conditional rules whose context block
    # this update actually carries, so the model isn't handed ~15 rules it
    # must ignore. The unconditional rules + tail are untouched.
    present_keys = set((update.context or {}).keys())
    template = _assemble_prompt(_load_prompt_template("analyzer"), present_keys)
    return template.format(
        source=update.source,
        subject=update.subject,
        current_version=update.current_version,
        new_version=update.new_version,
        release_url=update.release_url or "(none)",
        context=_serialize_context(update.context),
        notes=notes.strip() or "(no relevant notes)",
        release_notes=(update.release_notes or "(no release notes)").strip()[:15000],
    )


def _analysis_json_schema() -> dict:
    """JSON Schema for the `Analysis` object — used for guided / constrained
    decoding when `cfg.json_schema` is on.

    Hand-built rather than dumped from `Analysis.model_json_schema()` so it
    stays free of pydantic's `$defs` / `$ref` / `anyOf` constructs: OpenAI's
    strict mode and llama.cpp / Ollama grammar conversion are all happier
    with a flat schema. `tests/test_json_schema_mode.py` asserts this
    property set matches the `Analysis` model so the two can't drift.

    Every field is `required` with `additionalProperties: false`, which is
    what OpenAI strict mode demands and what the grammar backends accept
    unchanged. The list fields accept `[]` and `recommended_action` accepts
    `null`, so requiring them costs the model nothing.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "severity": {
                "type": "string",
                "enum": [s.value for s in Severity],
            },
            "summary": {"type": "string"},
            "breaking_changes": {"type": "array", "items": {"type": "string"}},
            "config_obsolete": {"type": "array", "items": {"type": "string"}},
            "new_features_relevant": {"type": "array", "items": {"type": "string"}},
            "action_required": {"type": "boolean"},
            "recommended_action": {"type": ["string", "null"]},
        },
        "required": [
            "severity",
            "summary",
            "breaking_changes",
            "config_obsolete",
            "new_features_relevant",
            "action_required",
            "recommended_action",
        ],
    }


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
        # Backends (keyed by provider+endpoint+model) that 4xx'd on a
        # json_schema response_format. Once a key lands here we stop
        # attempting schema mode for it and go straight to plain JSON, so a
        # misconfigured `json_schema=true` probes the backend exactly once
        # instead of doubling every request. Cleared on process restart;
        # changing the model/endpoint produces a new key that re-probes.
        self._schema_unsupported: set[tuple[str, str, str]] = set()

    @property
    def cfg(self) -> LLMConfig:
        return self._provider()

    def is_enabled(self) -> bool:
        return self.cfg.provider != "disabled"

    def _schema_key(self) -> tuple[str, str, str]:
        return (self.cfg.provider, self.cfg.endpoint, self.cfg.model)

    def _want_schema(self, strict_json: bool) -> bool:
        """Whether to send a guided-decoding json_schema for this call.

        Only when the user opted in (`cfg.json_schema`), the call wants JSON
        at all (`strict_json`), and the current backend hasn't already been
        recorded as rejecting schema mode.
        """
        return (
            strict_json
            and self.cfg.json_schema
            and self._schema_key() not in self._schema_unsupported
        )

    def _mark_schema_unsupported(self, status: int) -> None:
        log.warning(
            "LLM backend %s (%s) rejected json_schema response_format "
            "(HTTP %d); falling back to plain-JSON mode for this model. Set "
            "llm.json_schema=false to silence, or point at a backend that "
            "supports guided decoding.",
            self.cfg.endpoint, self.cfg.model, status,
        )
        self._schema_unsupported.add(self._schema_key())

    async def analyze(self, update: Update, notes: str = "") -> Analysis | None:
        self.last_call = None
        if not self.is_enabled():
            return None
        # Pre-LLM secret-leak guard: redact API keys / tokens / SSH keys from
        # notes + context + release notes before assembling the prompt. ON by
        # default for cloud providers, OFF for local/disabled. Override via
        # `cfg.secret_guard` (true/false/None for default).
        guarded_update = update
        guarded_notes = notes
        from .secret_guard import redact_context, redact_text, should_guard
        guard_override = getattr(self.cfg, "secret_guard", None)
        if should_guard(self.cfg.provider, override=guard_override):
            from .secret_guard import GuardReport
            rep = GuardReport()
            new_notes, _ = redact_text(notes, report=rep)
            new_release_notes, _ = redact_text(
                update.release_notes or "", report=rep,
            )
            new_ctx, _ = redact_context(update.context or {}, report=rep)
            if rep.redacted_count:
                log.info(
                    "secret_guard: redacted %d items (%s) for %s",
                    rep.redacted_count, ",".join(rep.matched_categories),
                    update.subject,
                )
            guarded_update = update.model_copy(update={
                "release_notes": new_release_notes,
                "context": new_ctx,
            })
            guarded_notes = new_notes
        prompt = build_prompt(guarded_update, notes=guarded_notes)
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
        # Ollama's `format` accepts the string "json" (loose) OR a full JSON
        # schema object (guided decoding, since Ollama 0.5). Pick per config.
        use_schema = self._want_schema(strict_json)
        base = {
            "model": self.cfg.model,
            "prompt": prompt,
            "stream": False,
            "options": {"num_ctx": self.cfg.context_size, "temperature": temperature},
        }

        def _payload(schema_mode: bool) -> dict:
            if schema_mode:
                fmt: object = _analysis_json_schema()
            elif strict_json:
                fmt = "json"
            else:
                fmt = None
            p = {**base, "format": fmt}
            return {k: v for k, v in p.items() if v is not None}

        async with httpx.AsyncClient(timeout=self.cfg.timeout) as client:
            try:
                r = await client.post(url, json=_payload(use_schema))
                r.raise_for_status()
            except httpx.HTTPStatusError as e:
                if not (use_schema and e.response.status_code in (400, 404, 422)):
                    raise
                self._mark_schema_unsupported(e.response.status_code)
                r = await client.post(url, json=_payload(False))
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
        use_schema = self._want_schema(strict_json)
        base = {
            "model": self.cfg.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        }

        def _payload(schema_mode: bool) -> dict:
            if schema_mode:
                rf: object = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "analysis",
                        "schema": _analysis_json_schema(),
                        "strict": True,
                    },
                }
            elif strict_json:
                rf = {"type": "json_object"}
            else:
                rf = None
            p = {**base, "response_format": rf}
            return {k: v for k, v in p.items() if v is not None}

        async with httpx.AsyncClient(timeout=self.cfg.timeout) as client:
            try:
                r = await client.post(url, json=_payload(use_schema), headers=headers)
                r.raise_for_status()
            except httpx.HTTPStatusError as e:
                if not (use_schema and e.response.status_code in (400, 404, 422)):
                    raise
                self._mark_schema_unsupported(e.response.status_code)
                r = await client.post(url, json=_payload(False), headers=headers)
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
        # Best-effort fallback. For an update advisor, failing *down* to
        # info hides danger: a row the model tried to flag critical would
        # drop below every push threshold and, because dedup skips rows
        # that already have an analysis, never get re-analysed. So keep the
        # severity the model emitted if it's a valid value, otherwise fail
        # *up* to high, and mark the summary so the degraded parse is
        # visible to the user.
        if "summary" not in data:
            return None
        raw_sev = data.get("severity")
        severity = Severity.HIGH
        if isinstance(raw_sev, str):
            try:
                severity = Severity(raw_sev.lower())
            except ValueError:
                severity = Severity.HIGH
        return Analysis(
            severity=severity,
            summary=f"[parse-degraded] {str(data['summary'])[:500]}",
        )
