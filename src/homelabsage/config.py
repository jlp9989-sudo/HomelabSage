"""Config loader — reads YAML, expands `${VAR}` from env / `.env`."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

# `${VAR}` or `${VAR:-default}`
_ENV_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-(.*?))?\}")


def _expand(value: Any) -> Any:
    """Recursively expand `${VAR}` placeholders against os.environ."""
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            var, default = m.group(1), m.group(2) or ""
            return os.environ.get(var, default)
        return _ENV_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _load_dotenv(path: Path) -> None:
    """Minimal `.env` loader — no extra dep, no quoting magic."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


# ─── Schema ──────────────────────────────────────────────────────────


class LLMConfig(BaseModel):
    provider: str = "ollama"
    endpoint: str = "http://localhost:11434"
    model: str = "qwen3:30b"
    context_size: int = 32768
    api_key: str = ""
    timeout: int = 180
    strict_json: bool = True


class DockerSourceConfig(BaseModel):
    enabled: bool = False
    socket: str = "/var/run/docker.sock"
    overrides: dict[str, str] = Field(default_factory=dict)
    skip: list[str] = Field(default_factory=list)
    # Containers `Status=exited` for at least this many days are flagged on each
    # Update via `context.orphan_since_days`. The analyzer prompt can pick that
    # up to suggest "is this container still needed?". Set to 0 to disable.
    orphan_min_days: int = 30


class HAConfig(BaseModel):
    enabled: bool = False
    url: str = "http://homeassistant.local:8123"
    token: str = ""
    include_hacs: bool = True
    include_addons: bool = True


class SourcesConfig(BaseModel):
    docker: DockerSourceConfig = Field(default_factory=DockerSourceConfig)
    homeassistant: HAConfig = Field(default_factory=HAConfig)
    # Placeholders for future plugins — kept loose to not break configs.
    fedora: dict[str, Any] = Field(default_factory=lambda: {"enabled": False})
    llamacpp: dict[str, Any] = Field(default_factory=lambda: {"enabled": False})
    huggingface_models: dict[str, Any] = Field(default_factory=lambda: {"enabled": False})
    unraid: dict[str, Any] = Field(default_factory=lambda: {"enabled": False})
    rss_feeds: dict[str, Any] = Field(default_factory=lambda: {"enabled": False})


class NotionOutputConfig(BaseModel):
    enabled: bool = False
    api_key: str = ""
    database_id: str = ""
    write_policy: str = "always"  # always | only_action_required


class TelegramOutputConfig(BaseModel):
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""
    min_severity: str = "high"

    @field_validator("min_severity")
    @classmethod
    def _check_sev(cls, v: str) -> str:
        if v not in {"critical", "high", "medium", "info"}:
            raise ValueError(f"min_severity must be critical|high|medium|info, got {v!r}")
        return v


class OutputsConfig(BaseModel):
    notion: NotionOutputConfig = Field(default_factory=NotionOutputConfig)
    telegram: TelegramOutputConfig = Field(default_factory=TelegramOutputConfig)


class SchedulerConfig(BaseModel):
    enabled: bool = True
    cron: str = "0 9 * * *"
    timezone: str = "UTC"
    heartbeat_url: str = ""


class WebAuthConfig(BaseModel):
    """HTTP Basic Auth for the web UI.

    Disabled by default so first-time users see the UI. Enable in any
    install that's bound to a non-loopback address. The `/healthz`
    endpoint is always unauthenticated (Docker healthcheck).
    """

    enabled: bool = False
    username: str = "admin"
    password: str = ""


class WebConfig(BaseModel):
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8000
    auth: WebAuthConfig = Field(default_factory=WebAuthConfig)


class StorageConfig(BaseModel):
    database_path: str = "./data/state.sqlite"


class NotesConfig(BaseModel):
    """User's homelab notes — extra context for the LLM.

    `notes_dir`: directory scanned for .md/.txt files; sections are matched
    against each update's subject so the LLM gets just the relevant pieces.

    `extra_docs`: a few files always injected in full (e.g. CLAUDE.md).
    Keep these short — they cost tokens on every LLM call.
    """

    notes_dir: str = ""
    extra_docs: list[str] = Field(default_factory=list)
    max_chars: int = 4000


class CuratorConfig(BaseModel):
    """Curator — generates one Markdown note per container so the rest of
    the pipeline always has fresh, written-up context to feed the LLM.

    `output_dir`: where notes are written. If empty, falls back to
    `notes.notes_dir` (the same directory the analyzer reads from).

    `prompt_template_path`: path to a custom .txt prompt template. If empty,
    the built-in default is used. The template may include the placeholders
    listed in `curator.PROMPT_PLACEHOLDERS`; unknown placeholders are kept
    verbatim so missing data never crashes the run.

    `style_examples`: paths to existing .md notes to inject as few-shot
    examples (e.g. `notes/example.md`). Optional — leave empty to skip.

    `recent_releases`: how many recent GitHub releases to forward to the LLM
    as upstream context.

    `max_release_chars`: per-prompt cap on combined release-notes text.
    """

    enabled: bool = True
    output_dir: str = ""
    prompt_template_path: str = ""
    style_examples: list[str] = Field(default_factory=list)
    recent_releases: int = 5
    max_release_chars: int = 8000


class Config(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    outputs: OutputsConfig = Field(default_factory=OutputsConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    notes: NotesConfig = Field(default_factory=NotesConfig)
    curator: CuratorConfig = Field(default_factory=CuratorConfig)


def load_config(path: str | Path) -> Config:
    """Load YAML config + .env from the same directory."""
    path = Path(path)
    _load_dotenv(path.parent / ".env")
    raw = yaml.safe_load(path.read_text()) or {}
    return Config(**_expand(raw))
