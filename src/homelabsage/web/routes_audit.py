"""Proactive auditor — `/audit` HTML + `/api/audit` JSON.

Read-only: the report is rebuilt on every request from the DB + live
parity probe. Cheap (no outbound HTTP), no caching needed for a homelab
scan tempo.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment

from ..audit import build_report, render_markdown
from ..config import Config
from ..db import Database


def register_audit_routes(app: FastAPI, cfg: Config, db: Database, env: Environment) -> None:
    @app.get("/audit", response_class=HTMLResponse)
    async def audit_page() -> HTMLResponse:
        report = build_report(cfg, db)
        body_md = render_markdown(report)
        tmpl = env.get_template("audit.html")
        return HTMLResponse(tmpl.render(report=report, body_md=body_md))

    @app.get("/api/audit")
    async def audit_api() -> JSONResponse:
        report = build_report(cfg, db)
        return JSONResponse(report.to_json())
