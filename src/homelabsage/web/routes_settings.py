"""HTTP API for inspecting and editing the configuration.

Exposes the Pydantic config tree as a small JSON API so the web UI (and any
other client) can read the current effective settings, write changes to the
user overlay (`config.user.yaml`), and revert individual fields back to the
deploy default.

Endpoints (all under `/api/settings`):

  GET    /api/settings                      Index of editable blocks.
  GET    /api/settings/{block}              Current values (secrets masked)
                                            + which fields come from the
                                            overlay + the block's JSON schema.
  PATCH  /api/settings/{block}              Partial update to the overlay
                                            for that block. Body is the block's
                                            shape, not the full Config.
  DELETE /api/settings/{block}              Remove the entire block from the
                                            overlay (revert to deploy default).
  DELETE /api/settings/{block}/{key:path}   Remove a single dotted key from
                                            the overlay.

Block names use `/` as a separator inside the URL when the Pydantic path is
nested (`sources/docker`, `outputs/notion`). The internal mapping lives in
`SETTING_BLOCKS` below; pass that to `register_settings_routes`.

Step 3 / v0.5 scope:
  - We write the overlay file atomically and re-validate the full Config
    against Pydantic before persisting. Invalid patches return 400 with the
    Pydantic error untouched.
  - We DO NOT hot-reload the in-memory engine after a write. The returned
    `current` block reflects the new file state, but services that captured
    config at startup (LLMClient, plugins, scheduler) still use the old
    values until the process restarts. Hot-reload is later work (v0.5 step 5+).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from pydantic import BaseModel, ValidationError

from ..config import (
    AppriseOutputConfig,
    AuditAlertsConfig,
    AutoApplyConfig,
    BackupHealthConfig,
    BatchingConfig,
    ComposeLintConfig,
    Config,
    CuratorConfig,
    DigestConfig,
    DiscordOutputConfig,
    DiskPressureConfig,
    DockerSourceConfig,
    FedoraSourceConfig,
    GitHubWatchedConfig,
    GotifyOutputConfig,
    HAConfig,
    HealthCheckConfig,
    I18nConfig,
    ImagePinsConfig,
    LLMConfig,
    LLMHealthGateConfig,
    LogAnomalyConfig,
    MSTeamsOutputConfig,
    NotesConfig,
    NotionOutputConfig,
    NtfyOutputConfig,
    ParityGateConfig,
    PushoverOutputConfig,
    ScanWindowConfig,
    SchedulerConfig,
    ScriptsSourceConfig,
    SlackOutputConfig,
    SMTPOutputConfig,
    StorageConfig,
    TagLagConfig,
    TelegramOutputConfig,
    TLSCheckConfig,
    WebAuthConfig,
    WebConfig,
    WebhookOutputConfig,
    load_config,
)
from ..config_overlay import (
    atomic_write_yaml,
    deep_merge,
    load_overlay,
    overlay_keys,
    remove_dotted,
    set_dotted,
    user_overlay_path,
)
from ..redact import _field_is_secret

log = logging.getLogger(__name__)


def _serialise_validation_errors(e: ValidationError) -> list[dict[str, Any]]:
    """Convert Pydantic's structured errors into a JSON-safe payload.

    Pydantic v2 emits validator-raised exceptions as `ctx={"error": <Exception>}`
    which is not JSON-serialisable. We stringify the value so FastAPI's
    `HTTPException` body survives the encoder.
    """
    out: list[dict[str, Any]] = []
    for err in e.errors():
        clean = dict(err)
        ctx = clean.get("ctx")
        if isinstance(ctx, dict):
            clean["ctx"] = {
                k: (str(v) if isinstance(v, BaseException) else v)
                for k, v in ctx.items()
            }
        out.append(clean)
    return out


# URL segment → (dotted path inside Config, Pydantic submodel class).
# Order in this dict drives the order returned by `GET /api/settings`.
SETTING_BLOCKS: dict[str, tuple[str, type[BaseModel]]] = {
    "llm": ("llm", LLMConfig),
    "sources/docker": ("sources.docker", DockerSourceConfig),
    "sources/homeassistant": ("sources.homeassistant", HAConfig),
    "sources/github_watched": ("sources.github_watched", GitHubWatchedConfig),
    "sources/fedora": ("sources.fedora", FedoraSourceConfig),
    "sources/scripts": ("sources.scripts", ScriptsSourceConfig),
    # ── outputs ──────────────────────────────────────────────────
    "outputs/notion": ("outputs.notion", NotionOutputConfig),
    "outputs/telegram": ("outputs.telegram", TelegramOutputConfig),
    "outputs/discord": ("outputs.discord", DiscordOutputConfig),
    "outputs/ntfy": ("outputs.ntfy", NtfyOutputConfig),
    "outputs/gotify": ("outputs.gotify", GotifyOutputConfig),
    "outputs/apprise": ("outputs.apprise", AppriseOutputConfig),
    "outputs/smtp": ("outputs.smtp", SMTPOutputConfig),
    "outputs/slack": ("outputs.slack", SlackOutputConfig),
    "outputs/msteams": ("outputs.msteams", MSTeamsOutputConfig),
    "outputs/pushover": ("outputs.pushover", PushoverOutputConfig),
    "outputs/webhook": ("outputs.webhook", WebhookOutputConfig),
    "outputs/batching": ("outputs.batching", BatchingConfig),
    # ── runtime ──────────────────────────────────────────────────
    "scheduler": ("scheduler", SchedulerConfig),
    "digest": ("digest", DigestConfig),
    "parity_gate": ("parity_gate", ParityGateConfig),
    "scan_window": ("scan_window", ScanWindowConfig),
    "llm_health_gate": ("llm_health_gate", LLMHealthGateConfig),
    "auto_apply": ("auto_apply", AutoApplyConfig),
    "image_pins": ("image_pins", ImagePinsConfig),
    "compose_lint": ("compose_lint", ComposeLintConfig),
    "tag_lag": ("tag_lag", TagLagConfig),
    "tls_check": ("tls_check", TLSCheckConfig),
    "disk_pressure": ("disk_pressure", DiskPressureConfig),
    "audit_alerts": ("audit_alerts", AuditAlertsConfig),
    "backup_health": ("backup_health", BackupHealthConfig),
    "health_check": ("health_check", HealthCheckConfig),
    "log_anomaly": ("log_anomaly", LogAnomalyConfig),
    "i18n": ("i18n", I18nConfig),
    "web": ("web", WebConfig),
    "web/auth": ("web.auth", WebAuthConfig),
    "storage": ("storage", StorageConfig),
    "notes": ("notes", NotesConfig),
    "curator": ("curator", CuratorConfig),
}


# Display grouping for the settings index page. A flat grid of 38 cards
# reads as "this product is complicated" — grouping tells a new user which
# handful of blocks they actually need and which 30 are opt-in extras they
# can ignore. UI-only: the JSON API and block URLs are unaffected.
#
# Every SETTING_BLOCKS key must appear in EXACTLY one group — a test guards
# this so a new block can't silently fall off the index page.
SETTING_GROUPS: list[dict[str, Any]] = [
    {
        "title": "Essentials",
        "description": (
            "What a working install needs: the LLM that reads release "
            "notes, the Docker scan, when to run it, and the notes that "
            "teach the analyzer about YOUR setup."
        ),
        "blocks": ["llm", "sources/docker", "scheduler", "notes", "curator"],
    },
    {
        "title": "More sources",
        "description": "Optional things to scan beyond Docker containers.",
        "blocks": [
            "sources/homeassistant", "sources/github_watched",
            "sources/fedora", "sources/scripts",
        ],
    },
    {
        "title": "Notifications",
        "description": (
            "Where analyzed updates get delivered. Enable the one or two "
            "channels you use; Notion persists rows, the rest push."
        ),
        "blocks": [
            "outputs/notion", "outputs/telegram", "outputs/discord",
            "outputs/ntfy", "outputs/gotify", "outputs/apprise",
            "outputs/smtp", "outputs/slack", "outputs/msteams",
            "outputs/pushover", "outputs/webhook", "outputs/batching",
            "digest",
        ],
    },
    {
        "title": "Server",
        "description": "Web UI, auth, storage paths, language.",
        "blocks": ["web", "web/auth", "storage", "i18n"],
    },
    {
        "title": "Advanced (opt-in)",
        "description": (
            "Extra detectors and gates, all off by default. A fresh "
            "install works fine without touching any of these."
        ),
        "blocks": [
            "parity_gate", "scan_window", "llm_health_gate", "auto_apply",
            "image_pins", "compose_lint", "tag_lag", "tls_check",
            "disk_pressure", "audit_alerts", "backup_health",
            "health_check", "log_anomaly",
        ],
    },
]


# ─── helpers ──────────────────────────────────────────────────────────────

def _get_dotted(d: dict[str, Any], dotted: str) -> Any:
    """Return the value at `dotted` inside `d`, or `{}` if not present."""
    cur: Any = d
    for p in dotted.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return {}
        cur = cur[p]
    return cur


def _block_to_dotted_keys(block_data: dict[str, Any], *, prefix: str = "") -> list[str]:
    """Flatten a block's overlay subtree to dotted leaf keys local to the block."""
    return overlay_keys(block_data, prefix=prefix)


def _mask_value(v: Any) -> Any:
    """Mask a single value while preserving its outer shape so the UI knows
    whether the field is set (non-empty masked) or empty (empty container)."""
    if isinstance(v, str):
        return "***" if v else ""
    if isinstance(v, list):
        return ["***"] * len(v)
    if isinstance(v, dict):
        return {k: "***" for k in v}
    return v


def _mask_secrets(
    values: dict[str, Any], schema_props: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Replace values whose KEY name looks secret with a sentinel.

    Masking preserves shape: a non-empty string → `"***"`, a populated
    list → `["***", "***", …]`, a populated dict → `{k: "***", …}`. Empty
    containers stay empty. The UI uses that to render "configured" /
    "not set" without ever shipping the real secret over the wire.

    Pass `schema_props` (the Pydantic JSON Schema `properties` map for the
    same block) so fields explicitly tagged `json_schema_extra={"ui_secret":
    True}` are masked even when their name doesn't match `SECRET_KEY_MARKERS`
    (`webhook_url`, `urls`, `headers`, `user_key`).
    """
    out: dict[str, Any] = {}
    for k, v in values.items():
        prop = schema_props.get(k) if isinstance(schema_props, dict) else None
        if _field_is_secret(k, prop):
            out[k] = _mask_value(v)
        elif isinstance(v, dict):
            sub_props = prop.get("properties") if isinstance(prop, dict) else None
            out[k] = _mask_secrets(v, sub_props)
        else:
            out[k] = v
    return out


def _resolve_block(block: str) -> tuple[str, type[BaseModel]]:
    if block not in SETTING_BLOCKS:
        raise HTTPException(404, f"unknown settings block: {block}")
    return SETTING_BLOCKS[block]


def _validate_overlay(cfg_path: Path, overlay_data: dict[str, Any]) -> Config:
    """Re-parse `config.yaml` and apply `overlay_data` on top, then build a
    full `Config` object so Pydantic's validators fire. Returns the validated
    Config; raises `ValidationError` on rejection.

    We do NOT touch the on-disk overlay here — caller writes only after this
    succeeds. Keeps validation pure.
    """
    import yaml

    base = yaml.safe_load(cfg_path.read_text()) or {}
    if not isinstance(base, dict):
        base = {}
    merged = deep_merge(base, overlay_data)
    return Config(**merged)


# ─── routes ───────────────────────────────────────────────────────────────

def register_settings_routes(app: FastAPI, cfg: Config, cfg_path: Path | None) -> None:
    """Attach `/api/settings/*` routes.

    `cfg`      — the Config loaded at app startup (used for the read view).
    `cfg_path` — path to `config.yaml`. Required for write endpoints; when
                 None, PATCH/DELETE return 503 ("read-only mode").
    """

    def _current_block(block_name: str) -> dict[str, Any]:
        """Return the current effective values for `block_name` as a plain dict
        (no Pydantic wrapper). Re-read from disk so the response reflects any
        writes that may have just happened, not the startup snapshot."""
        dotted, submodel = _resolve_block(block_name)
        if cfg_path and cfg_path.exists():
            try:
                fresh = load_config(cfg_path)
            except Exception:
                fresh = cfg
        else:
            fresh = cfg
        cur: Any = fresh
        for p in dotted.split("."):
            cur = getattr(cur, p)
        # cur is a Pydantic model; dump to dict to play with masking + dotted keys
        if isinstance(cur, BaseModel):
            return cur.model_dump(mode="json")
        return cur

    def _current_overlay() -> dict[str, Any]:
        if not cfg_path:
            return {}
        return load_overlay(user_overlay_path(cfg_path))

    @app.get("/api/settings")
    async def settings_index() -> dict[str, Any]:
        overlay = _current_overlay()
        flat_overrides = overlay_keys(overlay)
        return {
            "blocks": [
                {
                    "id": name,
                    "dotted_path": dotted,
                    "title": submodel.__name__,
                    "has_overrides": any(k.startswith(dotted) for k in flat_overrides),
                }
                for name, (dotted, submodel) in SETTING_BLOCKS.items()
            ],
            "overlay_path": str(user_overlay_path(cfg_path)) if cfg_path else None,
            "overlay_writable": cfg_path is not None,
            "overrides": flat_overrides,
        }

    @app.get("/api/settings/{block:path}")
    async def settings_get(block: str) -> dict[str, Any]:
        dotted, submodel = _resolve_block(block)
        current = _current_block(block)
        overlay = _current_overlay()
        block_overlay = _get_dotted(overlay, dotted)
        local_overrides = _block_to_dotted_keys(block_overlay)
        schema = submodel.model_json_schema()
        return {
            "block": block,
            "dotted_path": dotted,
            "current": _mask_secrets(current, schema.get("properties")),
            "overrides": local_overrides,
            "schema": schema,
        }

    # Hoisted Body sentinel — ruff B008 dislikes Body() in default-arg position,
    # and the FastAPI docs recommend the same pattern (module/closure-level).
    _patch_body = Body(...)

    @app.patch("/api/settings/{block:path}")
    async def settings_patch(
        block: str, body: dict[str, Any] = _patch_body,
    ) -> dict[str, Any]:
        if cfg_path is None:
            raise HTTPException(503, "settings UI is read-only (no config path)")
        dotted, _ = _resolve_block(block)
        if not isinstance(body, dict):
            raise HTTPException(400, "PATCH body must be a JSON object")

        overlay = _current_overlay()
        new_overlay = overlay
        # Apply each leaf in the body at <dotted>.<leaf>.
        for leaf_path in overlay_keys(body):
            value: Any = body
            for p in leaf_path.split("."):
                value = value[p]
            new_overlay = set_dotted(new_overlay, f"{dotted}.{leaf_path}", value)

        try:
            _validate_overlay(cfg_path, new_overlay)
        except ValidationError as e:
            raise HTTPException(400, {"validation_errors": _serialise_validation_errors(e)}) from e

        atomic_write_yaml(user_overlay_path(cfg_path), new_overlay)
        log.info("settings: PATCH %s wrote keys %s", block, list(body))
        return await settings_get(block)

    @app.delete("/api/settings/{block:path}")
    async def settings_delete(block: str) -> dict[str, Any]:
        if cfg_path is None:
            raise HTTPException(503, "settings UI is read-only (no config path)")
        # Two URL shapes resolve to this endpoint:
        #   /api/settings/llm                  → wipe the whole block
        #   /api/settings/llm/model            → wipe a single key
        # Distinguish by checking whether `block` is in SETTING_BLOCKS as-is
        # (whole-block delete) or whether splitting off the trailing segment
        # yields a known block (key delete).
        overlay = _current_overlay()
        if block in SETTING_BLOCKS:
            dotted, _ = SETTING_BLOCKS[block]
            new_overlay = remove_dotted(overlay, dotted)
            log.info("settings: DELETE %s wiped block", block)
        else:
            # Try peeling segments off the right end until we hit a known block.
            for split_at in range(len(block) - 1, -1, -1):
                if block[split_at] != "/":
                    continue
                head, tail = block[:split_at], block[split_at + 1:]
                if head in SETTING_BLOCKS:
                    dotted, _ = SETTING_BLOCKS[head]
                    new_overlay = remove_dotted(overlay, f"{dotted}.{tail}")
                    log.info("settings: DELETE %s removed key %s", head, tail)
                    block = head
                    break
            else:
                raise HTTPException(404, f"unknown settings block or key: {block}")

        try:
            _validate_overlay(cfg_path, new_overlay)
        except ValidationError as e:
            raise HTTPException(400, {"validation_errors": _serialise_validation_errors(e)}) from e

        atomic_write_yaml(user_overlay_path(cfg_path), new_overlay)
        return await settings_get(block)
