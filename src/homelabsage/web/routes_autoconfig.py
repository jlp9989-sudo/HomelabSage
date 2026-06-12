"""`/autoconfig` — review and apply host-detected config proposals.

The web face of `homelabsage autoconfig`: GET inspects the host (Docker
socket + filesystem probes) and renders every proposal with its evidence
and a checkbox; POST applies the selected subset to the user overlay via
the same validated write path the settings UI uses.

  GET  /autoconfig            review page (runs detection)
  POST /autoconfig/apply      apply selected proposals (form: `keys`)
  GET  /api/autoconfig        proposals as JSON (agents / scripts)

Detection talks to the Docker socket (blocking SDK) — both entry points
offload to a worker thread so the event loop stays responsive.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment
from pydantic import ValidationError

from ..autoconfig import apply_proposals, build_proposals
from ..config import Config, load_config

log = logging.getLogger(__name__)


def register_autoconfig_routes(
    app: FastAPI, cfg: Config, cfg_path: Path | None, env: Environment,
) -> None:
    def _fresh_cfg() -> Config:
        if cfg_path is not None and cfg_path.exists():
            try:
                return load_config(cfg_path)
            except Exception:
                pass
        return cfg

    def _render(
        proposals: list, *, flash: str = "", error: str = "",
    ) -> HTMLResponse:
        tmpl = env.get_template("autoconfig.html")
        return HTMLResponse(tmpl.render(
            proposals=[p.to_dict() for p in proposals],
            writable=cfg_path is not None,
            flash=flash, error=error,
        ))

    @app.get("/autoconfig", response_class=HTMLResponse)
    async def autoconfig_page() -> HTMLResponse:
        proposals = await asyncio.to_thread(build_proposals, _fresh_cfg())
        return _render(proposals)

    @app.post("/autoconfig/apply", response_class=HTMLResponse)
    async def autoconfig_apply(request: Request) -> HTMLResponse:
        if cfg_path is None:
            raise HTTPException(
                503, "autoconfig is read-only (no writable config path)",
            )
        form = await request.form()
        selected = {str(v) for v in form.getlist("keys")}
        fresh = _fresh_cfg()
        proposals = await asyncio.to_thread(build_proposals, fresh)
        if not selected:
            return _render(proposals, error="Nothing selected.")
        try:
            applied = await asyncio.to_thread(
                apply_proposals, cfg_path, proposals, only=selected,
            )
        except ValidationError as e:
            log.warning("autoconfig apply rejected: %s", e)
            return _render(
                proposals,
                error=f"Validation rejected the change: {e.error_count()} error(s).",
            )
        # Re-detect against the new on-disk config so applied proposals
        # disappear from the list instead of re-rendering stale state.
        remaining = await asyncio.to_thread(build_proposals, _fresh_cfg())
        return _render(
            remaining,
            flash=(
                f"Applied {len(applied)} proposal(s) to the user overlay. "
                f"Restart the service so the engine picks them up."
            ),
        )

    @app.get("/api/autoconfig")
    async def autoconfig_api() -> dict:
        proposals = await asyncio.to_thread(build_proposals, _fresh_cfg())
        return {"proposals": [p.to_dict() for p in proposals]}
