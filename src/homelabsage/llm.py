"""LLM client — Ollama-compatible (works with Ollama, llama.cpp server, LM Studio…).

Sends a structured prompt asking for JSON output. Falls back gracefully if the
LLM is unreachable or returns malformed JSON.
"""

from __future__ import annotations

import json
import logging
import re

import httpx
from pydantic import ValidationError

from .config import LLMConfig
from .models import Analysis, Severity, Update

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


PROMPT_TEMPLATE = """\
You analyze software updates for a homelab user.

Your output decisions must take into account:
  1. The release notes (what changed upstream)
  2. The user's current container/config snapshot
  3. The user's homelab notes — past decisions, custom builds, versionlocks,
     dependencies between services, known traps. These reflect REAL constraints
     that may turn an otherwise harmless update into a breaking one for this user.

For the update below, output a STRICT JSON object with EXACTLY these keys:
  - "severity": one of "critical" | "high" | "medium" | "info"
  - "summary": short paragraph (2-3 sentences), no markdown
  - "breaking_changes": list of short strings describing breaking changes that affect THIS user's setup
  - "config_obsolete": list of short strings describing parts of the user's current setup that the new version makes redundant
  - "new_features_relevant": list of short strings with new features likely useful for THIS user (cite the note if it informed your choice)
  - "action_required": boolean — true if the user MUST do something before/after updating
  - "recommended_action": short string with the next step, or null

Rules:
- Be concise. No filler.
- Output ONLY the JSON object. No prose, no markdown fences.
- If release notes are empty or uninformative, return severity "info" with empty arrays.
- Severity "critical" only for security CVEs or data-loss risk.
- "breaking_changes" must be filtered to the user's actual setup. Generic breaking changes
  irrelevant to them go in "config_obsolete" or are omitted.
- If the user's notes explicitly versionlock or warn against this update, raise severity
  by one step and mention the note in the summary.
- If the user's notes indicate the update depends on / will break another service
  (e.g. an upstream library), reflect it in breaking_changes or recommended_action.

# Update
- Source: {source}
- Subject: {subject}
- Current version: {current_version}
- New version: {new_version}
- Release URL: {release_url}

# User's current setup / config for this subject
{context}

# User's homelab notes (relevant excerpts)
{notes}

# Release notes
{release_notes}
"""


def build_prompt(update: Update, notes: str = "") -> str:
    return PROMPT_TEMPLATE.format(
        source=update.source,
        subject=update.subject,
        current_version=update.current_version,
        new_version=update.new_version,
        release_url=update.release_url or "(none)",
        context=json.dumps(update.context, indent=2, default=str) if update.context else "(none)",
        notes=notes.strip() or "(no relevant notes)",
        release_notes=(update.release_notes or "(no release notes)").strip()[:15000],
    )


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
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg

    def is_enabled(self) -> bool:
        return self.cfg.provider != "disabled"

    async def analyze(self, update: Update, notes: str = "") -> Analysis | None:
        if not self.is_enabled():
            return None
        prompt = build_prompt(update, notes=notes)
        try:
            raw = await self._call(prompt, strict_json=self.cfg.strict_json)
        except Exception as e:
            log.warning("LLM call failed for %s: %s", update.subject, e)
            return None
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
        if self.cfg.provider == "ollama":
            raw = await self._call_ollama(
                prompt, strict_json=strict_json, temperature=temperature
            )
        elif self.cfg.provider in {"openai", "anthropic"}:
            raw = await self._call_openai_compat(
                prompt, strict_json=strict_json, temperature=temperature
            )
        else:
            raise ValueError(f"unknown LLM provider: {self.cfg.provider}")
        return _strip_think_blocks(raw)

    async def _call_ollama(
        self, prompt: str, strict_json: bool, temperature: float
    ) -> str:
        """Ollama-compat: POST /api/generate, format=json forces JSON output."""
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
            return r.json().get("response", "")

    async def _call_openai_compat(
        self, prompt: str, strict_json: bool, temperature: float
    ) -> str:
        """OpenAI-compatible chat completions."""
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
            return data["choices"][0]["message"]["content"]


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
