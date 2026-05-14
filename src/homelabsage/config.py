"""Config loader — reads YAML, expands `${VAR}` from env / `.env`."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

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


class DockerSourceConfig(BaseModel):
    enabled: bool = False
    socket: str = "/var/run/docker.sock"
    overrides: dict[str, str] = Field(default_factory=dict)
    skip: list[str] = Field(default_factory=list)
    # Containers `Status=exited` for at least this many days are flagged on each
    # Update via `context.orphan_since_days`. The analyzer prompt can pick that
    # up to suggest "is this container still needed?". Set to 0 to disable.
    orphan_min_days: int = 30
    # Look up alternative images on Docker Hub + LSIO and attach them to each
    # Update's context. Disabled by default — costs 2-3 HTTP calls per
    # container per scan (Docker Hub search + LSIO catalog + tag enrichment).
    # When the network is rate-limited or unreachable the call returns empty
    # and the scan continues normally. See `homelabsage.images.Criteria` for
    # the per-call thresholds (10x pulls ratio, 90-day freshness, …).
    find_alternatives: bool = False
    # Classify each repo as alive/stale/abandoned (GitHub `pushed_at` and
    # `archived` flag) and attach to Update.context.repo_health. Enabled by
    # default because it's one extra GET per Update we'd already emit — the
    # GitHub rate-limit budget covers this without raising costs. Set to
    # false if you want to skip the extra request.
    repo_health: bool = True


class HAConfig(BaseModel):
    enabled: bool = False
    url: str = Field(
        "http://homeassistant.local:8123",
        description="Base URL of Home Assistant. LAN: `http://homeassistant.local:8123` or `http://<ip>:8123`.",
    )
    token: str = Field(
        "",
        description=(
            "Long-lived access token. Generate in HA: click your profile (bottom-left) → "
            "Security tab → 'Long-lived access tokens' → Create."
        ),
    )
    include_hacs: bool = Field(
        True,
        description="Include HACS-managed integrations (depends on the HACS sensor being exposed).",
    )
    include_addons: bool = Field(
        True,
        description="Include HA OS add-ons in the scan (requires Supervisor; non-OS installs return empty).",
    )


class ScriptsSourceConfig(BaseModel):
    """Enumerate cron jobs / systemd timers / Unraid User Scripts on the host.

    Universal across distros: every probe gracefully no-ops when its target
    directory or binary is missing, so the same defaults work on Debian,
    Fedora, Arch, Alpine, Unraid, macOS, etc.

    `etc_root` / `cron_spool` / `unraid_scripts_root` exist for the rare
    case where the relevant directories live under a non-default prefix
    (e.g. a chroot or a mounted host filesystem at `/host/etc`).
    """

    enabled: bool = False
    etc_root: str = "/etc"
    cron_spool: str = "/var/spool/cron"
    unraid_scripts_root: str = "/boot/config/plugins/user.scripts/scripts"
    systemctl_path: str = "systemctl"
    enable_systemd: bool = True
    enable_unraid: bool = False


class SourcesConfig(BaseModel):
    docker: DockerSourceConfig = Field(default_factory=DockerSourceConfig)
    homeassistant: HAConfig = Field(default_factory=HAConfig)
    scripts: ScriptsSourceConfig = Field(default_factory=ScriptsSourceConfig)
    # Placeholders for future plugins — kept loose to not break configs.
    fedora: dict[str, Any] = Field(default_factory=lambda: {"enabled": False})
    llamacpp: dict[str, Any] = Field(default_factory=lambda: {"enabled": False})
    huggingface_models: dict[str, Any] = Field(default_factory=lambda: {"enabled": False})
    unraid: dict[str, Any] = Field(default_factory=lambda: {"enabled": False})
    rss_feeds: dict[str, Any] = Field(default_factory=lambda: {"enabled": False})


class NotionOutputConfig(BaseModel):
    enabled: bool = False
    api_key: str = Field(
        "",
        description=(
            "Internal Integration Secret. Create one at "
            "https://www.notion.so/profile/integrations, then SHARE the target "
            "database with the integration (the secret alone is not enough)."
        ),
    )
    database_id: str = Field(
        "",
        description=(
            "32-char hex id from the database URL. Open the database in Notion, "
            "click ⋯ → Copy link; the id is the 32 chars after the last `/` and "
            "before the `?`."
        ),
    )
    write_policy: Literal["always", "only_action_required"] = Field(
        "always",
        description="`always` writes every analyzed update; `only_action_required` skips info-level ones.",
    )


class TelegramOutputConfig(BaseModel):
    enabled: bool = False
    bot_token: str = Field(
        "",
        description="Token from @BotFather. Talk to him in Telegram, /newbot, paste the token he gives you.",
    )
    chat_id: str = Field(
        "",
        description=(
            "Numeric chat id. For personal chat: talk to @userinfobot and use the `Id` it returns. "
            "For groups: add the bot, send `/start@yourbot`, then call `getUpdates`."
        ),
    )
    min_severity: Literal["critical", "high", "medium", "info"] = Field(
        "high",
        description="Only push updates at or above this severity. Use `critical` to get CVEs only.",
    )


class OutputsConfig(BaseModel):
    notion: NotionOutputConfig = Field(default_factory=NotionOutputConfig)
    telegram: TelegramOutputConfig = Field(default_factory=TelegramOutputConfig)


class SchedulerConfig(BaseModel):
    enabled: bool = True
    cron: str = Field(
        "0 9 * * *",
        description=(
            "When to run an automatic scan. Pick a preset or set a custom "
            "5-field cron expression (`min hour day month weekday`)."
        ),
        json_schema_extra={"ui_widget": "cron"},
    )
    timezone: str = Field(
        "UTC",
        description="IANA timezone the cron runs against, e.g. `Europe/Madrid`, `America/New_York`.",
        json_schema_extra={"ui_widget": "timezone"},
    )
    heartbeat_url: str = Field(
        "",
        description=(
            "Optional URL pinged with GET after every successful scan, e.g. "
            "an Uptime Kuma push monitor. Leave empty to disable."
        ),
    )

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        """Reject typos at save time rather than at scheduler-start time.

        Uses stdlib `zoneinfo`, which reads the system tzdata. We declare
        the `tzdata` PyPI package as a dependency so minimal OS images
        without an OS-level tz database (Alpine, distroless, Windows)
        still resolve every IANA name including aliases like `US/Pacific`.
        """
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(
                f"unknown timezone {v!r} — must be a valid IANA name like "
                f"'Europe/Madrid', 'America/New_York', or 'UTC'"
            ) from e
        return v


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
    database_path: str = Field(
        "./data/state.sqlite",
        description="SQLite file for run state. Absolute path or relative to the working directory.",
        json_schema_extra={"ui_widget": "path", "ui_path_kind": "file"},
    )


class NotesConfig(BaseModel):
    """User's homelab notes — extra context for the LLM.

    `notes_dir`: directory scanned for .md/.txt files; sections are matched
    against each update's subject so the LLM gets just the relevant pieces.

    `extra_docs`: a few files always injected in full (e.g. CLAUDE.md).
    Keep these short — they cost tokens on every LLM call.
    """

    notes_dir: str = Field(
        "",
        description="Directory scanned for .md/.txt files. Leave empty to skip notes injection.",
        json_schema_extra={"ui_widget": "path", "ui_path_kind": "directory"},
    )
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
    output_dir: str = Field(
        "",
        description="Where curator notes are written. Leave empty to write into `notes.notes_dir`.",
        json_schema_extra={"ui_widget": "path", "ui_path_kind": "directory"},
    )
    prompt_template_path: str = Field(
        "",
        description="Path to a custom prompt .txt. Leave empty to use the built-in default.",
        json_schema_extra={"ui_widget": "path", "ui_path_kind": "file"},
    )
    style_examples: list[str] = Field(default_factory=list)
    recent_releases: int = 5
    max_release_chars: int = 8000

    # Enrichment knobs — extra context fetched per container before the LLM
    # is called. Each fetch is best-effort; failures don't break the curate.
    fetch_readme: bool = Field(
        True,
        description="Fetch the upstream repo's README.md (raw.githubusercontent.com) and inject an excerpt into the curator prompt.",
    )
    fetch_docker_hub: bool = Field(
        True,
        description="When the image lives on Docker Hub, fetch its `full_description` as a fallback context source.",
    )
    include_logs: bool = Field(
        True,
        description="Include the container's last ~30 log lines as live context. Disable if you don't want logs forwarded to the LLM.",
    )
    max_readme_chars: int = 8000
    max_docker_hub_chars: int = 6000
    max_logs_chars: int = 3000
    log_tail_lines: int = 30


class Config(BaseModel):
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
    from .config_overlay import deep_merge, load_overlay, user_overlay_path

    path = Path(path)
    _load_dotenv(path.parent / ".env")
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raw = {}
    overlay = load_overlay(user_overlay_path(path))
    merged = deep_merge(raw, overlay) if overlay else raw
    return Config(**_expand(merged))
