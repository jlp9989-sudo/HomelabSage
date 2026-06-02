"""'What I see' diagnostics page.

  GET /diagnostics            HTML view: per-container verdict + counts
  GET /api/diagnostics        JSON view for scripting

Reads docker state through `homelabsage.diagnostics` on every request. The
probe is cheap (no outbound HTTP) so we don't bother caching.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment

from ..config import Config
from ..diagnostics import diagnose_containers, summarise


def register_diagnostics_routes(app: FastAPI, cfg: Config, env: Environment) -> None:
    @app.get("/diagnostics", response_class=HTMLResponse)
    async def diagnostics_page() -> HTMLResponse:
        diagnostics = diagnose_containers(cfg.sources.docker)
        counts = summarise(diagnostics)
        tmpl = env.get_template("diagnostics.html")
        return HTMLResponse(tmpl.render(diagnostics=diagnostics, counts=counts))

    @app.get("/api/diagnostics")
    async def diagnostics_api() -> JSONResponse:
        diagnostics = diagnose_containers(cfg.sources.docker)
        return JSONResponse({
            "counts": summarise(diagnostics),
            "rows": [d.to_dict() for d in diagnostics],
        })
