"""Candidate model definitions for the routing eval.

Privacy: this file is public, so it hardcodes NO private endpoints, LAN IPs,
or API keys. Local-model names + endpoint are supplied at run time (CLI flag
or env); cloud entries use public model IDs and pull keys from standard env
vars. A cloud candidate is only built when its key is present.

Every candidate forces `json_schema=True`: the whole point is to measure each
model under the guided-decoding path the router uses in production.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..config import LLMConfig

# Local-model defaults: a local llama-server is OpenAI-compatible and
# swaps models on demand by name (`--models-max`). We talk to it through the
# `ollama` provider only if it's the raw Ollama API; llama-server speaks the
# OpenAI shape, so default to that. Endpoint + model list come from outside.
DEFAULT_LOCAL_ENDPOINT = os.environ.get(
    "HLS_EVAL_LOCAL_ENDPOINT", "http://localhost:11434",
)
LOCAL_TIMEOUT = int(os.environ.get("HLS_EVAL_LOCAL_TIMEOUT", "300"))


@dataclass
class FreeCloudModel:
    provider: str          # LLMConfig.provider value
    model: str             # public model ID
    env_key: str           # env var holding the API key
    note: str = ""


# Public free-tier models worth testing. Slugs drift over time (especially
# OpenRouter ':free' variants) — override with --cloud-model if one 404s.
FREE_CLOUD_MODELS: list[FreeCloudModel] = [
    FreeCloudModel("gemini", "gemini-2.5-flash", "GEMINI_API_KEY"),
    FreeCloudModel("gemini", "gemini-2.5-flash-lite", "GEMINI_API_KEY"),
    FreeCloudModel("groq", "llama-3.3-70b-versatile", "GROQ_API_KEY"),
    FreeCloudModel(
        "groq", "meta-llama/llama-4-scout-17b-16e-instruct", "GROQ_API_KEY",
    ),
    FreeCloudModel(
        "openrouter", "deepseek/deepseek-chat-v3-0324:free", "OPENROUTER_API_KEY",
        note="rate-limited; measure validity not representative latency",
    ),
    FreeCloudModel(
        "openrouter", "qwen/qwen3-235b-a22b:free", "OPENROUTER_API_KEY",
        note="rate-limited; measure validity not representative latency",
    ),
]


@dataclass
class Candidate:
    label: str             # row label in the matrix
    config: LLMConfig
    note: str = ""


def local_candidate(model: str, *, endpoint: str | None = None) -> Candidate:
    """One local llama-server model as a candidate."""
    return Candidate(
        label=f"local/{model}",
        config=LLMConfig(
            provider="openai",  # llama-server speaks OpenAI chat-completions
            endpoint=endpoint or DEFAULT_LOCAL_ENDPOINT,
            model=model,
            api_key=os.environ.get("HLS_EVAL_LOCAL_API_KEY", ""),
            timeout=LOCAL_TIMEOUT,
            strict_json=True,
            json_schema=True,
        ),
        note="local llama-server; big models slow on cold swap",
    )


def cloud_candidate(m: FreeCloudModel) -> Candidate | None:
    """Build a cloud candidate, or None when its API key isn't in the env."""
    key = os.environ.get(m.env_key, "")
    if not key:
        return None
    return Candidate(
        label=f"{m.provider}/{m.model}",
        config=LLMConfig(
            provider=m.provider,
            endpoint="",  # falls back to PROVIDER_PRESETS default
            model=m.model,
            api_key=key,
            timeout=120,
            strict_json=True,
            json_schema=True,
        ),
        note=m.note,
    )


def _preset_endpoint(provider: str) -> str:
    from ..llm import PROVIDER_PRESETS
    return PROVIDER_PRESETS.get(provider, {}).get("endpoint", "")


def build_candidates(
    *,
    local_models: list[str],
    local_endpoint: str | None = None,
    include_cloud: bool = True,
) -> list[Candidate]:
    """Assemble the candidate set: every local model + every cloud model
    whose API key is present in the environment."""
    out: list[Candidate] = [
        local_candidate(m, endpoint=local_endpoint) for m in local_models
    ]
    if include_cloud:
        for m in FREE_CLOUD_MODELS:
            c = cloud_candidate(m)
            if c is not None:
                # Fill the preset endpoint so the row records where it went.
                if not c.config.endpoint:
                    c.config.endpoint = _preset_endpoint(m.provider)
                out.append(c)
    return out
