"""First-run wizard — guided onboarding for new HomelabSage installs.

A 5-screen flow (welcome → LLM → Docker → Scheduler → done) that walks a
non-technical user through the bare minimum needed to land on the first
real scan. Optional outputs (Notion / Telegram) are NOT in the wizard —
they go through `/settings` after the install is functional. The wizard's
job is "first useful run", not "configure everything".

Persistence model:
  - Each step POST writes its block to `config.user.yaml` via the same
    overlay machinery the settings UI uses, so a half-finished wizard
    leaves a valid partial config behind.
  - Completion writes a `wizard.done` marker file alongside the overlay.
    The dashboard checks this marker to decide whether to show the
    "Get started" banner. The marker is intentionally NOT inside the
    overlay so a future Pydantic strict-mode toggle can't reject it,
    and the settings index's override count stays clean.

This module deliberately reuses the field-rendering machinery from
`routes_settings_html` (`_block_form_context`, `_form_to_block_patch`)
to avoid drift — adding a new field to e.g. `LLMConfig` will show up in
both the wizard step and the regular settings form with zero changes
here.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment
from pydantic import ValidationError

from ..config import Config, load_config
from ..config_overlay import (
    atomic_write_yaml,
    load_overlay,
    set_dotted,
    user_overlay_path,
)
from .routes_settings import _resolve_block, _validate_overlay
from .routes_settings_html import (
    _block_form_context,
    _form_to_block_patch,
    _format_pydantic_errors,
)

log = logging.getLogger(__name__)


# Step order. Keys are URL slugs (`/wizard/<slug>`), values describe the
# step. `block` is the SETTING_BLOCKS key — the field-rendering code uses
# it. `skippable=True` shows a "Skip this step" button.
WIZARD_STEPS: list[dict[str, Any]] = [
    {
        "slug": "llm",
        "block": "llm",
        "title": "Connect an LLM",
        "intro": (
            "HomelabSage uses an LLM to read release notes and decide if "
            "an update is worth your attention. Pick a provider below; "
            "the endpoint and a sensible model are filled in automatically. "
            "Local? Pick `ollama`. Cheap cloud? `groq` or `gemini` give "
            "you a free tier."
        ),
        "skippable": False,
    },
    {
        "slug": "docker",
        "block": "sources/docker",
        "title": "Scan your Docker containers",
        "intro": (
            "Docker is the primary update source. Toggle it on, and "
            "HomelabSage will read your running containers (image, tag, "
            "labels) directly from the socket. No agent, no inventory file."
        ),
        "skippable": False,
    },
    {
        "slug": "scheduler",
        "block": "scheduler",
        "title": "Schedule the scan (optional)",
        "intro": (
            "Automate a daily/weekly scan, or skip this and run manually "
            "from the dashboard. You can change this later under Settings."
        ),
        "skippable": True,
    },
]


def _step_index(slug: str) -> int:
    for i, step in enumerate(WIZARD_STEPS):
        if step["slug"] == slug:
            return i
    raise HTTPException(404, f"unknown wizard step: {slug}")


def _next_step_url(current_slug: str) -> str:
    """URL of the screen the user should see after finishing `current_slug`."""
    i = _step_index(current_slug)
    if i + 1 < len(WIZARD_STEPS):
        return f"/wizard/{WIZARD_STEPS[i + 1]['slug']}"
    return "/wizard/done"


def wizard_marker_path(cfg_path: Path) -> Path:
    """Sibling marker file: `config.yaml` → `wizard.done` in the same dir.

    Kept OUT of the YAML overlay so:
      - the overlay stays a pure config representation (no UI state),
      - the settings page's "override count" doesn't include a meta key,
      - a future Pydantic strict-mode toggle can't reject it on load.
    """
    return Path(cfg_path).resolve().parent / "wizard.done"


def is_wizard_complete(cfg_path: Path | None) -> bool:
    """Returns True if the user has already finished or skipped the wizard.

    No cfg_path (read-only mode) → True, because we can't show a useful
    wizard without a writable overlay, so the dashboard banner must hide.
    """
    if cfg_path is None:
        return True
    return wizard_marker_path(cfg_path).exists()


def mark_wizard_complete(cfg_path: Path) -> None:
    """Touch the marker file. Idempotent."""
    wizard_marker_path(cfg_path).write_text("ok\n", encoding="utf-8")


# ─── route registration ────────────────────────────────────────────────


def register_wizard_routes(
    app: FastAPI, cfg: Config, cfg_path: Path | None, env: Environment
) -> None:
    """Attach `/wizard/*` routes."""

    def _render(template: str, **ctx: Any) -> HTMLResponse:
        tmpl = env.get_template(template)
        return HTMLResponse(tmpl.render(nav="wizard", **ctx))

    def _step_ctx(step: dict[str, Any]) -> dict[str, Any]:
        """Build the context for a step page: progress + form fields."""
        i = _step_index(step["slug"])
        block_ctx = _block_form_context(cfg, cfg_path, step["block"])
        return {
            "step": step,
            "step_number": i + 1,
            "total_steps": len(WIZARD_STEPS),
            "next_url": _next_step_url(step["slug"]),
            **block_ctx,
        }

    @app.get("/wizard", response_class=HTMLResponse)
    async def wizard_welcome() -> HTMLResponse:
        return _render(
            "wizard.html",
            view="welcome",
            total_steps=len(WIZARD_STEPS),
            steps=WIZARD_STEPS,
            overlay_writable=cfg_path is not None,
        )

    # `skip-all` MUST be registered before the `{slug}` catch-all below,
    # otherwise FastAPI matches `POST /wizard/{slug}` first (slug="skip-all")
    # and the handler 404s because there's no step named "skip-all".
    @app.post("/wizard/skip-all", response_class=HTMLResponse)
    async def wizard_skip_all() -> RedirectResponse:
        """User clicked 'I'll configure manually' on the welcome screen."""
        if cfg_path is not None:
            mark_wizard_complete(cfg_path)
        return RedirectResponse("/", status_code=303)

    @app.get("/wizard/{slug}", response_class=HTMLResponse)
    async def wizard_step_view(slug: str) -> HTMLResponse:
        if slug == "done":
            return _render(
                "wizard.html",
                view="done",
                total_steps=len(WIZARD_STEPS),
                overlay_writable=cfg_path is not None,
                already_complete=is_wizard_complete(cfg_path),
            )
        step = WIZARD_STEPS[_step_index(slug)]
        return _render("wizard.html", view="step", **_step_ctx(step))

    @app.post("/wizard/{slug}", response_class=HTMLResponse)
    async def wizard_step_submit(request: Request, slug: str) -> HTMLResponse:
        if slug == "done":
            # POST /wizard/done = "mark complete and bail out to the dashboard"
            if cfg_path is not None:
                mark_wizard_complete(cfg_path)
            return RedirectResponse("/", status_code=303)

        step = WIZARD_STEPS[_step_index(slug)]
        form = dict(await request.form())

        # "Skip this step" sends a marker field; we accept and move on
        # without touching the overlay for this block.
        if form.pop("_wizard_skip", None):
            if not step["skippable"]:
                raise HTTPException(400, "this step is not skippable")
            return RedirectResponse(_next_step_url(slug), status_code=303)

        if cfg_path is None:
            raise HTTPException(503, "wizard is read-only (no writable config path)")

        dotted, submodel = _resolve_block(step["block"])
        fresh_cfg = load_config(cfg_path) if cfg_path.exists() else cfg
        cur_obj: Any = fresh_cfg
        for p in dotted.split("."):
            cur_obj = getattr(cur_obj, p)
        current_real = cur_obj.model_dump(mode="json")

        try:
            patch = _form_to_block_patch(form, submodel.model_json_schema(), current_real)
        except HTTPException as e:
            ctx = _step_ctx(step)
            ctx["error"] = str(e.detail)
            return _render("wizard.html", view="step", **ctx)

        # Apply the patch via the same path the settings UI uses.
        overlay = load_overlay(user_overlay_path(cfg_path))
        new_overlay = overlay
        from ..config_overlay import overlay_keys as _ok
        for leaf in _ok(patch):
            value: Any = patch
            for p in leaf.split("."):
                value = value[p]
            new_overlay = set_dotted(new_overlay, f"{dotted}.{leaf}", value)
        try:
            _validate_overlay(cfg_path, new_overlay)
        except ValidationError as e:
            ctx = _step_ctx(step)
            ctx["error"] = _format_pydantic_errors(e)
            return _render("wizard.html", view="step", **ctx)

        atomic_write_yaml(user_overlay_path(cfg_path), new_overlay)
        return RedirectResponse(_next_step_url(slug), status_code=303)
