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


class WebConfig(BaseModel):
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8000


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


class Config(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    outputs: OutputsConfig = Field(default_factory=OutputsConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    notes: NotesConfig = Field(default_factory=NotesConfig)


def load_config(path: str | Path) -> Config:
    """Load YAML config + .env from the same directory."""
    path = Path(path)
    _load_dotenv(path.parent / ".env")
    raw = yaml.safe_load(path.read_text()) or {}
    return Config(**_expand(raw))
