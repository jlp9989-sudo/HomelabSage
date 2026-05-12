"""LLM profile management UI (v0.5 step 4 extension).

Lets the user define multiple named LLMConfigs and switch the active one
without editing YAML. Lives on top of the existing `config.user.yaml`
overlay layer — no new persistence surface.

URL shape:

  GET  /settings/llm/profiles             list + add-new form
  POST /settings/llm/profiles/save        upsert one profile (name in form body)
  POST /settings/llm/profiles/{name}/activate   make `name` the active one
  POST /settings/llm/profiles/{name}/delete     remove the profile

Data lives at `cfg.llm_profiles[name]` (a dict[str, LLMConfig]) plus
`cfg.llm_active: str`. `homelabsage.config.get_active_llm_config` resolves
whichever is in effect. Switching active is a one-key overlay write; the
engine's LLMClient picks up the change on its next call (hot-reload).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment
from pydantic import ValidationError

from ..config import Config, LLMConfig, load_config
from ..config_overlay import (
    atomic_write_yaml,
    load_overlay,
    remove_dotted,
    set_dotted,
    user_overlay_path,
)
from ..redact import _is_secret_key
from .routes_settings import _validate_overlay
from .routes_settings_html import _coerce_form_value, _is_htmx

log = logging.getLogger(__name__)


def _profile_form_fields(profile: LLMConfig | None) -> list[dict[str, Any]]:
    """Build the same `fields` shape the schema-driven renderer uses, so the
    profile edit form can reuse `_settings_form.html`-style rendering."""
    schema = LLMConfig.model_json_schema()
    current = profile.model_dump(mode="json") if profile else {}
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
        is_bool = prop_type == "boolean"
        is_int = prop_type == "integer"
        widget = prop.get("ui_widget", "")
        display = "" if (raw_value is None or is_secret) else str(raw_value)
        fields.append({
            "name": name,
            "title": prop.get("title", name),
            "description": prop.get("description", ""),
            "type": prop_type or "string",
            "widget": widget,
            "is_secret": is_secret,
            "is_array": False,
            "is_object": False,
            "is_bool": is_bool,
            "is_int": is_int,
            "is_override": False,
            "value": display,
            "bool_value": bool(raw_value) if is_bool else False,
            "default": prop.get("default"),
            "placeholder": (
                "(configured — leave blank to keep)" if (is_secret and raw_value)
                else ("(not set)" if is_secret else "")
            ),
        })
    return fields


def _build_llm_from_form(
    form: dict[str, str], existing: LLMConfig | None
) -> dict[str, Any]:
    """Coerce form values to the LLMConfig shape; preserve existing secret
    values when the user submits an empty input (same rule as the main
    settings form)."""
    schema = LLMConfig.model_json_schema()
    props = schema.get("properties", {})
    out: dict[str, Any] = {}
    for name, prop_schema in props.items():
        is_bool = (
            prop_schema.get("type") == "boolean"
            or any(s.get("type") == "boolean" for s in prop_schema.get("anyOf", []))
        )
        if name not in form:
            if is_bool:
                out[name] = False
            continue
        raw = form[name]
        if _is_secret_key(name) and raw == "":
            # Keep the previous secret rather than blanking it.
            if existing is not None:
                out[name] = getattr(existing, name)
            continue
        try:
            out[name] = _coerce_form_value(raw, prop_schema)
        except ValueError as e:
            raise HTTPException(400, f"field {name!r}: {e}") from e
    return out


def register_llm_profiles_routes(
    app: FastAPI, cfg: Config, cfg_path: Path | None, env: Environment
) -> None:
    def _fresh_cfg() -> Config:
        return load_config(cfg_path) if cfg_path and cfg_path.exists() else cfg

    def _write_overlay(new_overlay: dict[str, Any]) -> None:
        if cfg_path is None:
            raise HTTPException(503, "settings UI is read-only (no config path)")
        try:
            _validate_overlay(cfg_path, new_overlay)
        except ValidationError as e:
            raise HTTPException(400, str(e)) from e
        atomic_write_yaml(user_overlay_path(cfg_path), new_overlay)

    @app.get("/settings/llm/profiles", response_class=HTMLResponse)
    async def profiles_index(
        request: Request, edit: str | None = None, flash: str = "", error: str = ""
    ) -> HTMLResponse:
        fresh = _fresh_cfg()
        profiles = dict(fresh.llm_profiles)
        # If `edit` matches an existing profile, prefill its form; if it's a
        # non-empty string that doesn't match, treat as "create new with this
        # suggested name". Empty `edit` shows the add-new form blank.
        editing: tuple[str, LLMConfig | None]
        if edit is not None and edit in profiles:
            editing = (edit, profiles[edit])
        elif edit:
            editing = (edit, None)
        else:
            editing = ("", None)
        tmpl = env.get_template("llm_profiles.html")
        return HTMLResponse(tmpl.render(
            profiles=profiles,
            active=fresh.llm_active,
            edit_name=editing[0],
            edit_fields=_profile_form_fields(editing[1]),
            edit_is_new=editing[1] is None,
            flash=flash,
            error=error,
            overlay_writable=cfg_path is not None,
        ))

    @app.post("/settings/llm/profiles/save", response_class=HTMLResponse, response_model=None)
    async def profiles_save(request: Request) -> HTMLResponse | RedirectResponse:
        form = dict(await request.form())
        name = (form.pop("__profile_name", "") or "").strip()
        if not name:
            return await profiles_index(request, error="Profile name is required.")
        if not name.replace("-", "").replace("_", "").isalnum():
            return await profiles_index(
                request, edit=name,
                error=f"Profile name {name!r}: only letters, digits, hyphen, underscore allowed.",
            )
        fresh = _fresh_cfg()
        existing = fresh.llm_profiles.get(name)
        try:
            payload = _build_llm_from_form(form, existing)
        except HTTPException as e:
            return await profiles_index(request, edit=name, error=str(e.detail))

        overlay = load_overlay(user_overlay_path(cfg_path)) if cfg_path else {}
        new_overlay = set_dotted(overlay, f"llm_profiles.{name}", payload)
        try:
            _write_overlay(new_overlay)
        except HTTPException as e:
            return await profiles_index(request, edit=name, error=str(e.detail))

        if _is_htmx(request):
            return await profiles_index(request, flash=f"Profile {name!r} saved.")
        return RedirectResponse(
            f"/settings/llm/profiles?flash=Profile+{name!r}+saved.",
            status_code=303,
        )

    @app.post("/settings/llm/profiles/{name}/activate", response_class=HTMLResponse)
    async def profiles_activate(request: Request, name: str) -> HTMLResponse:
        fresh = _fresh_cfg()
        if name not in fresh.llm_profiles:
            return await profiles_index(request, error=f"Unknown profile {name!r}.")
        overlay = load_overlay(user_overlay_path(cfg_path)) if cfg_path else {}
        new_overlay = set_dotted(overlay, "llm_active", name)
        try:
            _write_overlay(new_overlay)
        except HTTPException as e:
            return await profiles_index(request, error=str(e.detail))
        return await profiles_index(request, flash=f"Active profile is now {name!r}.")

    @app.post("/settings/llm/profiles/{name}/delete", response_class=HTMLResponse)
    async def profiles_delete(request: Request, name: str) -> HTMLResponse:
        overlay = load_overlay(user_overlay_path(cfg_path)) if cfg_path else {}
        new_overlay = remove_dotted(overlay, f"llm_profiles.{name}")
        # If the deleted one was active, blank the active pointer so the engine
        # falls back to legacy `llm` rather than carrying a dangling reference.
        fresh = _fresh_cfg()
        if fresh.llm_active == name:
            new_overlay = set_dotted(new_overlay, "llm_active", "")
        try:
            _write_overlay(new_overlay)
        except HTTPException as e:
            return await profiles_index(request, error=str(e.detail))
        return await profiles_index(request, flash=f"Profile {name!r} deleted.")

    @app.post("/llm/active", response_class=HTMLResponse, response_model=None)
    async def dashboard_active_switch(request: Request) -> HTMLResponse | RedirectResponse:
        """Dashboard quick-switch: a single form field `active` that
        sets `llm_active` to either a profile name or empty (legacy `llm`).
        Returns a tiny fragment when called via HTMX, otherwise redirects
        back to the dashboard."""
        form = dict(await request.form())
        new_active = form.get("active", "")
        fresh = _fresh_cfg()
        if new_active and new_active not in fresh.llm_profiles:
            if _is_htmx(request):
                return HTMLResponse(
                    f'<div class="flash error">Unknown profile {new_active!r}.</div>',
                    status_code=400,
                )
            raise HTTPException(400, f"unknown profile: {new_active}")
        overlay = load_overlay(user_overlay_path(cfg_path)) if cfg_path else {}
        new_overlay = set_dotted(overlay, "llm_active", new_active)
        try:
            _write_overlay(new_overlay)
        except HTTPException as e:
            if _is_htmx(request):
                return HTMLResponse(
                    f'<div class="flash error">{e.detail}</div>', status_code=400
                )
            raise

        if _is_htmx(request):
            label = new_active or "(legacy llm)"
            return HTMLResponse(
                f'<span class="flash success" style="display:inline-block; padding:.2rem .6rem">'
                f'Active LLM → <strong>{label}</strong></span>'
            )
        return RedirectResponse("/", status_code=303)
