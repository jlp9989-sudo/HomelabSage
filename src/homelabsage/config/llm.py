"""LLM provider config — used both by the engine and the connection-test
endpoints. Splitting it from the rest of the config so the schema-driven
settings UI can render it independently."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class LLMConfig(BaseModel):
    provider: Literal[
        "ollama", "openai", "groq", "gemini", "openrouter", "anthropic", "disabled"
    ] = Field(
        "ollama",
        description=(
            "Which backend to talk to. Picking a provider auto-fills `endpoint` "
            "and `model` with that provider's standard values — if you had "
            "custom values typed, change the provider first and then re-edit "
            "those fields. `groq` / `gemini` / `openrouter` / `openai` all "
            "speak the OpenAI chat-completions shape; `ollama` speaks the raw "
            "Ollama API; `anthropic` uses Anthropic's OpenAI-compatible bridge."
        ),
    )
    endpoint: str = Field(
        "http://localhost:11434",
        description=(
            "Base URL of the LLM API. Examples: `http://192.168.1.10:11434` "
            "(local), `https://api.groq.com/openai`, "
            "`https://generativelanguage.googleapis.com/v1beta/openai` (Gemini)."
        ),
    )
    model: str = Field(
        "qwen3:30b",
        description="Model name as the backend expects it. For local models, ≥30B parameters recommended.",
    )
    context_size: int = Field(
        32768,
        description="Max prompt tokens the model can see in one call.",
    )
    api_key: str = Field(
        "",
        description="Bearer token for cloud providers; leave empty for local servers without auth.",
    )
    timeout: int = Field(
        180,
        description="Seconds before a single LLM call times out. Raise to 300+ if your local model loads on demand.",
    )
    strict_json: bool = Field(
        True,
        description="Force the model to return strict JSON (provider-specific). Disable only when the model rejects the response_format header.",
    )
    secret_guard: bool | None = Field(
        None,
        description=(
            "Pre-LLM secret-leak guard: redact API keys / tokens / SSH keys "
            "from prompts before sending. `null` (default) = on for cloud "
            "providers, off for local (`ollama` / `disabled`). `true` forces "
            "on, `false` forces off."
        ),
    )
