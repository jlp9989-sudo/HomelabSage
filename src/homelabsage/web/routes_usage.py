"""LLM-usage dashboard.

  GET /usage            HTML summary: per-provider/model totals + recent calls
  GET /api/usage        JSON shape consumed by automation / agents

The numbers come from the `llm_usage` table. Provider-reported tokens
when available; chars/4 estimate otherwise (`estimated=true` per row).
Cost columns are intentionally absent — pricing changes faster than this
repo could track, and the user already has the providers' own dashboards
for billing detail.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment

from ..db import Database


def register_usage_routes(app: FastAPI, db: Database, env: Environment) -> None:
    @app.get("/usage", response_class=HTMLResponse)
    async def usage_page() -> HTMLResponse:
        summary = db.usage_summary(days=30)
        recent = db.recent_llm_calls(limit=50)
        tmpl = env.get_template("usage.html")
        return HTMLResponse(tmpl.render(summary=summary, recent=recent))

    @app.get("/api/usage")
    async def usage_api() -> JSONResponse:
        return JSONResponse({
            "summary": db.usage_summary(days=30),
            "recent": db.recent_llm_calls(limit=50),
        })
