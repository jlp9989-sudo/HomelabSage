"""HomelabSage configuration root.

This module is a façade over the split submodules under `config/`. Every
public symbol that used to live in the flat `config.py` is re-exported
here so callers can keep doing `from homelabsage.config import LLMConfig`,
`from homelabsage.config import load_config`, etc.

The split is a pure refactor: behaviour is unchanged. See each submodule
for the rationale on grouping.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from ._env import _ENV_RE, _expand, _load_dotenv
from .llm import LLMConfig
from .outputs import (
    BatchingConfig,
    DiscordOutputConfig,
    GotifyOutputConfig,
    NotionOutputConfig,
    NtfyOutputConfig,
    OutputsConfig,
    TelegramOutputConfig,
)
from .runtime import (
    BackupHealthConfig,
    BackupRepoConfig,
    ComposeLintConfig,
    DigestConfig,
    HealthCheckConfig,
    I18nConfig,
    LogAnomalyConfig,
    ParityGateConfig,
    SchedulerConfig,
    TagLagConfig,
)
from .sources import (
    DockerSourceConfig,
    FedoraSourceConfig,
    GitHubWatchedConfig,
    HAConfig,
    ScriptsSourceConfig,
    SourcesConfig,
)
from .storage import CuratorConfig, NotesConfig, StorageConfig
from .web import WebAuthConfig, WebConfig


class Config(BaseModel):
    """Top-level configuration the engine consumes."""

    llm: LLMConfig = Field(default_factory=LLMConfig)
    # Optional multi-profile setup. `llm_profiles` maps a human-friendly
    # name to an LLMConfig; `llm_active` selects which one is in use. When
    # `llm_active` is empty OR doesn't match any profile, `llm` is used as
    # the fallback — so single-config setups keep working unchanged.
    #
    # Example YAML:
    #   llm_active: halo
    #   llm_profiles:
    #     halo:
    #       provider: openai
    #       endpoint: http://192.168.31.19:11434
    #       model: Qwen3.6-35B-Abl
    #     groq:
    #       provider: openai
    #       endpoint: https://api.groq.com/openai
    #       api_key: ${GROQ_API_KEY}
    #       model: llama-3.3-70b-versatile
    llm_profiles: dict[str, LLMConfig] = Field(default_factory=dict)
    llm_active: str = ""
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    outputs: OutputsConfig = Field(default_factory=OutputsConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    notes: NotesConfig = Field(default_factory=NotesConfig)
    curator: CuratorConfig = Field(default_factory=CuratorConfig)
    digest: DigestConfig = Field(default_factory=DigestConfig)
    parity_gate: ParityGateConfig = Field(default_factory=ParityGateConfig)
    backup_health: BackupHealthConfig = Field(default_factory=BackupHealthConfig)
    health_check: HealthCheckConfig = Field(default_factory=HealthCheckConfig)
    log_anomaly: LogAnomalyConfig = Field(default_factory=LogAnomalyConfig)
    compose_lint: ComposeLintConfig = Field(default_factory=ComposeLintConfig)
    tag_lag: TagLagConfig = Field(default_factory=TagLagConfig)
    i18n: I18nConfig = Field(default_factory=I18nConfig)


def get_active_llm_config(cfg: Config) -> LLMConfig:
    """Resolve which LLMConfig is in effect right now.

    Precedence:
      1. If `cfg.llm_active` names a key in `cfg.llm_profiles`, return that.
      2. Else fall back to the legacy single `cfg.llm`.

    This is intentionally permissive — a misspelled active name silently
    falls back rather than failing the run. The dashboard shows the
    effective profile name so the user notices.
    """
    if cfg.llm_active and cfg.llm_active in cfg.llm_profiles:
        return cfg.llm_profiles[cfg.llm_active]
    return cfg.llm


def load_config(path: str | Path) -> Config:
    """Load YAML config + .env + (optional) user-overlay from the same directory.

    Precedence: `config.yaml` (deploy default) → `config.user.yaml` (written
    by the web UI, optional). The overlay is deep-merged on top; absent
    overlay is a no-op. See `homelabsage.config_overlay` for the merge rules.

    Environment-variable interpolation runs AFTER the merge so the user
    overlay can introduce `${VAR}` placeholders too.
    """
    # Local import keeps the overlay module's import-time work out of the
    # hot path when callers only need the submodels.
    from ..config_overlay import deep_merge, load_overlay, user_overlay_path

    path = Path(path)
    _load_dotenv(path.parent / ".env")
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raw = {}
    overlay = load_overlay(user_overlay_path(path))
    merged = deep_merge(raw, overlay) if overlay else raw
    return Config(**_expand(merged))


__all__ = [
    # helpers
    "Config",
    "get_active_llm_config",
    "load_config",
    # submodels — re-exported for back-compat with `from homelabsage.config import …`
    "LLMConfig",
    "DockerSourceConfig",
    "HAConfig",
    "ScriptsSourceConfig",
    "FedoraSourceConfig",
    "GitHubWatchedConfig",
    "SourcesConfig",
    "NotionOutputConfig",
    "TelegramOutputConfig",
    "DiscordOutputConfig",
    "NtfyOutputConfig",
    "GotifyOutputConfig",
    "OutputsConfig",
    "BatchingConfig",
    "SchedulerConfig",
    "DigestConfig",
    "ParityGateConfig",
    "BackupHealthConfig",
    "BackupRepoConfig",
    "HealthCheckConfig",
    "LogAnomalyConfig",
    "ComposeLintConfig",
    "TagLagConfig",
    "I18nConfig",
    "WebAuthConfig",
    "WebConfig",
    "StorageConfig",
    "NotesConfig",
    "CuratorConfig",
    # private helpers that test files reach into
    "_expand",
    "_load_dotenv",
    "_ENV_RE",
]
