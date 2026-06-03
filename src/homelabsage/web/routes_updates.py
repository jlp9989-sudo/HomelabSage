"""Routes for the analyzed-updates dashboard.

  GET  /                  list of analyzed updates, severity-coloured
  POST /run               kick off a scan in the background
  POST /updates/<id>/status   apply / dismiss a row
  GET  /api/updates       JSON API for programmatic consumers
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment

from ..db import Database
from ..engine import Engine
from ..models import UpdateStatus
from .routes_wizard import is_wizard_complete


def register_updates_routes(
    app: FastAPI, db: Database, engine: Engine, env: Environment
) -> None:
    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        items = db.list(limit=500)
        # Group by status for the simple summary at the top
        counts = {s.value: 0 for s in UpdateStatus}
        for it in items:
            counts[it.status.value] += 1
        # Re-read cfg from disk so the profile dropdown reflects the
        # current state (the engine's hot-reload already covers LLM calls;
        # this is purely a UI freshness concern).
        fresh = engine.cfg
        if engine._cfg_path and engine._cfg_path.exists():
            try:
                from ..config import load_config
                fresh = load_config(engine._cfg_path)
            except Exception:
                pass
        # Show the "start the wizard" banner only on a fresh install: no
        # wizard marker yet AND no scan results to show. We deliberately
        # don't redirect — a banner is recoverable, a redirect surprises
        # the user and breaks bookmarks.
        show_wizard_banner = (
            not is_wizard_complete(engine._cfg_path) and not items
        )
        tmpl = env.get_template("index.html")
        return HTMLResponse(
            tmpl.render(
                items=items, counts=counts,
                llm_enabled=engine.llm.is_enabled(),
                llm_profiles=list(fresh.llm_profiles.keys()),
                llm_active=fresh.llm_active,
                show_wizard_banner=show_wizard_banner,
            )
        )

    @app.post("/run")
    async def trigger_run() -> RedirectResponse:
        asyncio.create_task(engine.run_once())
        return RedirectResponse("/", status_code=303)

    @app.post("/updates/{update_id:path}/status")
    async def set_status(
        update_id: str,
        status: str = Form(...),
        ack: str = Form(""),
    ) -> RedirectResponse:
        new_status = UpdateStatus(status)
        # Pre-flight gate: if the user is APPLYING and the analysis carries
        # breaking_changes, force a confirmation page. The gate is opt-in
        # via `web.preflight_gate` so single-user installs can keep the
        # one-click flow if they prefer.
        if (
            new_status == UpdateStatus.APPLIED
            and getattr(engine.cfg.web, "preflight_gate", False)
            and ack != "yes"
        ):
            it = db.get(update_id)
            if it and it.analysis and it.analysis.breaking_changes:
                return RedirectResponse(
                    f"/updates/{update_id}/preflight", status_code=303,
                )
        db.set_status(update_id, new_status)
        # Queue a post-update health probe when applying. Best-effort:
        # missing the container_name (non-docker source) silently skips.
        if new_status == UpdateStatus.APPLIED and engine.cfg.health_check.enabled:
            it = db.get(update_id)
            if it and it.update.source == "docker":
                from ..health_check import queue_for_update
                queue_for_update(
                    db, update_id=update_id,
                    container_name=it.update.subject,
                    cfg=engine.cfg.health_check,
                )
        return RedirectResponse("/", status_code=303)

    @app.get("/updates/{update_id:path}/preflight", response_class=HTMLResponse)
    async def preflight(update_id: str) -> HTMLResponse:
        """Confirmation wall shown when applying an update with breaking
        changes. Renders the breaking_changes + recommended_action and
        requires an explicit ack POST before the status flips."""
        it = db.get(update_id)
        if it is None:
            return HTMLResponse("Not found", status_code=404)
        tmpl = env.get_template("preflight.html")
        return HTMLResponse(tmpl.render(item=it))

    @app.get("/api/updates")
    async def api_updates() -> list[dict]:
        return [it.model_dump(mode="json") for it in db.list(limit=500)]
