"""HTML-rendered settings UI (v0.5 step 4).

The JSON API at `/api/settings/*` is the source of truth. These HTML routes
are a thin server-rendered layer on top so a non-technical user can edit
the same config from a browser, with no Python knowledge and no YAML.

Form rendering is schema-driven: we walk the Pydantic JSON Schema returned by
each block and emit the appropriate HTML input per `type` (string → text,
boolean → checkbox, integer → number, array → textarea one-per-line, object
→ textarea `key: value` per line). Adding a new field to a Pydantic submodel
is a zero-template-edit affair — the form picks it up automatically next
restart.

URL shape (mirrors the JSON API):

  GET  /settings                            Block index.
  GET  /settings/{block:path}               Block edit page.
  POST /settings/{block:path}/update        Form submit (HTMX or plain).
  POST /settings/{block:path}/revert        Wipe whole block from overlay.
  POST /settings/{block}/{key:path}/revert  Wipe a single field.

Each write-path endpoint responds with EITHER the full HTML page (for plain
form submits) OR just the form fragment (when `HX-Request: true` is set by
HTMX). That lets the UI swap inline without a full reload while keeping the
no-JS fallback working.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment
from pydantic import ValidationError

from ..config import Config, load_config
from ..config_overlay import (
    atomic_write_yaml,
    load_overlay,
    overlay_keys,
    remove_dotted,
    set_dotted,
    user_overlay_path,
)
from ..redact import _is_secret_key
from .routes_settings import (
    SETTING_BLOCKS,
    _block_to_dotted_keys,
    _get_dotted,
    _resolve_block,
    _validate_overlay,
)
from .routes_settings_test import BLOCK_TEST_ENDPOINTS

log = logging.getLogger(__name__)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request", "").lower() == "true"


def _coerce_form_value(raw: str, prop_schema: dict[str, Any]) -> Any:
    """Turn a form-encoded string into the right Python type for the schema.

    The schema's `type` (or first `anyOf` entry, when Optional is used) drives
    the coercion. Unknown types fall through as raw strings.

    For arrays of strings the raw value is one item per line, blanks
    dropped — the most common shape for the lists in this config (skip
    regex, mounts, extra_docs). For object types (Pydantic dict[str, str])
    the raw value is `key: value` per line, parsed as YAML.
    """
    t = prop_schema.get("type")
    if t is None and "anyOf" in prop_schema:
        # Optional fields appear as anyOf: [{type: X}, {type: null}].
        for opt in prop_schema["anyOf"]:
            if "type" in opt and opt["type"] != "null":
                t = opt["type"]
                break

    if t == "boolean":
        return raw.lower() in {"true", "on", "1", "yes"}
    if t == "integer":
        return int(raw)
    if t == "number":
        return float(raw)
    if t == "array":
        return [line.strip() for line in raw.splitlines() if line.strip()]
    if t == "object":
        try:
            parsed = yaml.safe_load(raw) or {}
        except yaml.YAMLError as e:
            raise ValueError(f"object field could not be parsed as YAML: {e}") from e
        if not isinstance(parsed, dict):
            raise ValueError(f"object field must be a key:value mapping, got {type(parsed).__name__}")
        return parsed
    return raw


def _form_to_block_patch(
    form: dict[str, str],
    schema: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    """Build the dict that gets sent to the overlay write.

    Rules:
      - Only fields present in the schema's `properties` are considered;
        anything else (CSRF tokens, layout helpers, …) is ignored.
      - Secret fields whose form value is empty are SKIPPED — submitting
        an unchanged form must NOT clobber the existing secret with "".
        If the user genuinely wants to clear a secret, they revert it.
      - Booleans: a checkbox that isn't checked is absent from form data
        entirely, so we default to False for any schema-declared bool that's
        missing from the form.
      - Type coercion failures propagate as ValueError, surfaced to the
        user as a 400.
    """
    props = schema.get("properties", {})
    out: dict[str, Any] = {}
    for name, prop_schema in props.items():
        is_bool = (
            prop_schema.get("type") == "boolean"
            or any(s.get("type") == "boolean" for s in prop_schema.get("anyOf", []))
        )
        if name not in form:
            # Missing checkbox → False; everything else means "no change".
            if is_bool:
                out[name] = False
            continue
        raw = form[name]
        if _is_secret_key(name) and raw == "":
            # Don't clobber an existing secret with the empty string.
            continue
        try:
            out[name] = _coerce_form_value(raw, prop_schema)
        except ValueError as e:
            raise HTTPException(400, f"field {name!r}: {e}") from e
    # Drop bool fields that match the current value (they shouldn't write to
    # the overlay just because the form happened to round-trip the same value).
    # The PATCH layer is idempotent so this is just a polish: smaller overlay.
    out = {k: v for k, v in out.items() if current.get(k) != v}
    return out


def _block_form_context(
    cfg: Config, cfg_path: Path | None, block: str
) -> dict[str, Any]:
    """Compute everything the form template needs in one shot."""
    dotted, submodel = _resolve_block(block)
    fresh = load_config(cfg_path) if cfg_path and cfg_path.exists() else cfg
    cur: Any = fresh
    for p in dotted.split("."):
        cur = getattr(cur, p)
    current = cur.model_dump(mode="json")
    overlay = load_overlay(user_overlay_path(cfg_path)) if cfg_path else {}
    local_overrides = set(_block_to_dotted_keys(_get_dotted(overlay, dotted)))
    schema = submodel.model_json_schema()

    # Build a per-field render hint table to keep the template simple.
    fields: list[dict[str, Any]] = []
    for name, prop in schema.get("properties", {}).items():
        prop_type = prop.get("type")
        if prop_type is None:
            for opt in prop.get("anyOf", []):
                if opt.get("type") and opt["type"] != "null":
                    prop_type = opt["type"]
                    break
        raw_value = current.get(name)
        is_secret = _is_secret_key(name) and prop_type == "string"
        is_array = prop_type == "array"
        is_object = prop_type == "object"
        is_bool = prop_type == "boolean"
        is_int = prop_type == "integer"
        # Pydantic emits `enum: [...]` for `Literal[...]` fields. When present,
        # the renderer picks a <select> instead of a free text input — gives
        # the form server-side validated values without a custom widget.
        enum_values = prop.get("enum") or []
        is_enum = bool(enum_values) and not is_bool

        # The textbox value for arrays/objects is the YAML/lines representation.
        if is_array and isinstance(raw_value, list):
            display = "\n".join(str(v) for v in raw_value)
        elif is_object and isinstance(raw_value, dict):
            display = yaml.safe_dump(raw_value, default_flow_style=False, sort_keys=True).strip()
        elif is_secret:
            # Already masked by the JSON API; for the form we show empty +
            # placeholder so an unchanged submit doesn't write "***" back.
            display = ""
        else:
            display = "" if raw_value is None else str(raw_value)

        # `ui_widget` lets a Pydantic Field opt into a custom renderer. The
        # template chooses `_widget_<name>.html` when this is set; otherwise
        # the default type-based rendering applies.
        widget = prop.get("ui_widget", "")
        path_kind = prop.get("ui_path_kind", "")

        # For the path widget: show the absolute path the value resolves to,
        # so the user knows where their data actually lives. `~` is NOT
        # expanded here because Python doesn't expand it at config-load time
        # either — keeping the preview honest about runtime behavior.
        resolved_path = ""
        if widget == "path" and isinstance(raw_value, str) and raw_value.strip():
            try:
                resolved_path = str(Path(raw_value).resolve(strict=False))
            except (OSError, RuntimeError):
                resolved_path = ""

        fields.append({
            "name": name,
            "title": prop.get("title", name),
            "description": prop.get("description", ""),
            "type": prop_type or "string",
            "widget": widget,
            "path_kind": path_kind,
            "resolved_path": resolved_path,
            "is_secret": is_secret,
            "is_array": is_array,
            "is_object": is_object,
            "is_bool": is_bool,
            "is_int": is_int,
            "is_enum": is_enum,
            "enum_values": enum_values,
            "is_override": name in local_overrides,
            "value": display,
            # Raw value useful for the bool checkbox state
            "bool_value": bool(raw_value) if is_bool else False,
            "default": prop.get("default"),
            "placeholder": "(configured — leave blank to keep)" if (is_secret and raw_value)
                           else ("(not set)" if is_secret else ""),
        })

    return {
        "block": block,
        "title": submodel.__name__,
        "fields": fields,
        "any_override": bool(local_overrides),
        "overlay_writable": cfg_path is not None,
        # When set, the form renders a "Test connection" button targeting
        # this URL. None for blocks that don't talk to an external service.
        "test_endpoint": BLOCK_TEST_ENDPOINTS.get(block),
    }


# ─── routes ───────────────────────────────────────────────────────────────

def register_settings_html_routes(
    app: FastAPI, cfg: Config, cfg_path: Path | None, env: Environment
) -> None:
    def _render_index() -> HTMLResponse:
        overlay = load_overlay(user_overlay_path(cfg_path)) if cfg_path else {}
        flat = set(overlay_keys(overlay))
        blocks = [
            {
                "id": name,
                "title": submodel.__name__,
                "dotted_path": dotted,
                "override_count": sum(1 for k in flat if k.startswith(dotted)),
            }
            for name, (dotted, submodel) in SETTING_BLOCKS.items()
        ]
        tmpl = env.get_template("settings_index.html")
        return HTMLResponse(
            tmpl.render(
                blocks=blocks,
                overlay_writable=cfg_path is not None,
                overlay_path=str(user_overlay_path(cfg_path)) if cfg_path else "",
                total_overrides=len(flat),
            )
        )

    def _render_block(block: str, *, flash: str = "", error: str = "",
                      partial: bool = False) -> HTMLResponse:
        ctx = _block_form_context(cfg, cfg_path, block)
        tmpl_name = "_settings_form.html" if partial else "settings_block.html"
        tmpl = env.get_template(tmpl_name)
        return HTMLResponse(tmpl.render(flash=flash, error=error, **ctx))

    def _apply_patch(block: str, body: dict[str, Any]) -> None:
        if cfg_path is None:
            raise HTTPException(503, "settings UI is read-only (no config path)")
        dotted, _ = _resolve_block(block)
        overlay = load_overlay(user_overlay_path(cfg_path))
        new_overlay = overlay
        for leaf in overlay_keys(body):
            value: Any = body
            for p in leaf.split("."):
                value = value[p]
            new_overlay = set_dotted(new_overlay, f"{dotted}.{leaf}", value)

        try:
            _validate_overlay(cfg_path, new_overlay)
        except ValidationError as e:
            raise HTTPException(400, _format_pydantic_errors(e)) from e

        atomic_write_yaml(user_overlay_path(cfg_path), new_overlay)

    def _apply_revert(block: str, key: str | None) -> None:
        if cfg_path is None:
            raise HTTPException(503, "settings UI is read-only (no config path)")
        dotted, _ = _resolve_block(block)
        overlay = load_overlay(user_overlay_path(cfg_path))
        target = dotted if key is None else f"{dotted}.{key}"
        new_overlay = remove_dotted(overlay, target)
        try:
            _validate_overlay(cfg_path, new_overlay)
        except ValidationError as e:
            raise HTTPException(400, _format_pydantic_errors(e)) from e
        atomic_write_yaml(user_overlay_path(cfg_path), new_overlay)

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_index() -> HTMLResponse:
        return _render_index()

    @app.get("/settings/{block:path}", response_class=HTMLResponse)
    async def settings_block_view(block: str) -> HTMLResponse:
        _resolve_block(block)  # 404 early if unknown
        return _render_block(block)

    @app.post("/settings/{block:path}/update", response_class=HTMLResponse)
    async def settings_update(request: Request, block: str) -> HTMLResponse:
        dotted, submodel = _resolve_block(block)
        form = dict(await request.form())

        # Walk the dotted path on the fresh config to get the current values
        # for the bool/diff-trim logic in `_form_to_block_patch`.
        fresh_cfg = load_config(cfg_path) if cfg_path and cfg_path.exists() else cfg
        cur_obj: Any = fresh_cfg
        for p in dotted.split("."):
            cur_obj = getattr(cur_obj, p)
        current_real = cur_obj.model_dump(mode="json")

        try:
            patch = _form_to_block_patch(form, submodel.model_json_schema(), current_real)
        except HTTPException as e:
            return _render_block(block, error=str(e.detail), partial=_is_htmx(request))

        try:
            _apply_patch(block, patch)
            flash, error = ("Saved." if patch else "No changes."), ""
        except HTTPException as e:
            flash, error = "", str(e.detail)

        return _render_block(block, flash=flash, error=error, partial=_is_htmx(request))

    @app.post("/settings/{rest:path}/revert", response_class=HTMLResponse)
    async def settings_revert(request: Request, rest: str) -> HTMLResponse:
        """One handler for both `/<block>/revert` and `/<block>/<key>/revert`.

        Disambiguates by walking known blocks longest-first: if `rest` is a
        known block, this is a whole-block revert; otherwise we split at the
        last slash that yields a known block, treating the suffix as the
        single-key revert target. Longest-first matters for nested blocks
        like `sources/docker/repo_health` (block `sources/docker`, key
        `repo_health`) vs the made-up `sources/docker_repo_health` block.
        """
        if rest in SETTING_BLOCKS:
            block, key = rest, None
        else:
            block, key = None, None
            for candidate in sorted(SETTING_BLOCKS, key=len, reverse=True):
                if rest.startswith(f"{candidate}/"):
                    block = candidate
                    key = rest[len(candidate) + 1:]
                    break
            if block is None:
                raise HTTPException(404, f"unknown settings block: {rest}")

        try:
            _apply_revert(block, key=key)
            flash = (
                f"Reverted {key} to deploy default."
                if key else "Block reverted to deploy default."
            )
            error = ""
        except HTTPException as e:
            flash, error = "", str(e.detail)
        return _render_block(block, flash=flash, error=error, partial=_is_htmx(request))


def _format_pydantic_errors(e: ValidationError) -> str:
    """Compress a ValidationError into a short human string for the form."""
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err.get("loc", []))
        msg = err.get("msg", "invalid")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts) or "validation failed"
