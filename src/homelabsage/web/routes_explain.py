"""Explain mode — "Why is this verdict?" surface.

  GET /updates/{update_id}/explain       HTML page with prompt + response
  GET /api/updates/{update_id}/explain   JSON for scripts / MCP debug

Renders the exact prompt sent to the LLM and the raw response received,
so the user can audit why a verdict landed where it did. Read-only —
the explainer table is populated by the engine.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment

from ..db import Database


def register_explain_routes(app: FastAPI, db: Database, env: Environment) -> None:
    @app.get("/updates/{update_id:path}/explain", response_class=HTMLResponse)
    async def explain_page(update_id: str) -> HTMLResponse:
        item = db.get(update_id)
        if item is None:
            raise HTTPException(404, f"unknown update: {update_id}")
        explainer = db.get_explainer(update_id)
        tmpl = env.get_template("explain.html")
        return HTMLResponse(tmpl.render(item=item, explainer=explainer))

    @app.get("/api/updates/{update_id:path}/explain")
    async def explain_api(update_id: str) -> JSONResponse:
        if db.get(update_id) is None:
            raise HTTPException(404, f"unknown update: {update_id}")
        explainer = db.get_explainer(update_id)
        if explainer is None:
            return JSONResponse(
                {"update_id": update_id, "available": False},
                status_code=404,
            )
        # Serialise the datetime for JSON cleanliness
        out = dict(explainer)
        if out.get("created_at"):
            out["created_at"] = out["created_at"].isoformat()
        out["available"] = True
        return JSONResponse(out)
