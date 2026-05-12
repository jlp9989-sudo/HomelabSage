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
        tmpl = env.get_template("index.html")
        return HTMLResponse(
            tmpl.render(
                items=items, counts=counts,
                llm_enabled=engine.llm.is_enabled(),
                llm_profiles=list(fresh.llm_profiles.keys()),
                llm_active=fresh.llm_active,
            )
        )

    @app.post("/run")
    async def trigger_run() -> RedirectResponse:
        asyncio.create_task(engine.run_once())
        return RedirectResponse("/", status_code=303)

    @app.post("/updates/{update_id:path}/status")
    async def set_status(update_id: str, status: str = Form(...)) -> RedirectResponse:
        db.set_status(update_id, UpdateStatus(status))
        return RedirectResponse("/", status_code=303)

    @app.get("/api/updates")
    async def api_updates() -> list[dict]:
        return [it.model_dump(mode="json") for it in db.list(limit=500)]
