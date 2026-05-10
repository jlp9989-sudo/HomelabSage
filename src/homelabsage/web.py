"""Web UI + background scheduler.

The web part is intentionally tiny: server-rendered HTML, no SPA. Each row of
the updates table has Apply/Dismiss buttons (regular POST forms). Add HTMX
later if you want live updates without page reloads.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import secrets
from pathlib import Path

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import Config
from .db import Database
from .engine import Engine
from .models import UpdateStatus
from .notes import NotesEditor

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(cfg: Config) -> FastAPI:
    app = FastAPI(title="HomelabSage", version="0.0.1", docs_url=None, redoc_url=None)
    db = Database(cfg.storage.database_path)
    engine = Engine(cfg, db)
    editor = NotesEditor(cfg.notes.notes_dir or None)
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
    )
    scheduler: AsyncIOScheduler | None = None

    # ─── HTTP Basic Auth middleware ────────────────────────────────────
    if cfg.web.auth.enabled and cfg.web.auth.password:
        expected_user = cfg.web.auth.username.encode()
        expected_pass = cfg.web.auth.password.encode()

        @app.middleware("http")
        async def basic_auth(request: Request, call_next):
            # Health endpoint must stay open for Docker / Kuma probes.
            if request.url.path == "/healthz":
                return await call_next(request)
            header = request.headers.get("authorization", "")
            if header.startswith("Basic "):
                try:
                    creds = base64.b64decode(header[6:]).decode()
                    user, _, pwd = creds.partition(":")
                    if (
                        secrets.compare_digest(user.encode(), expected_user)
                        and secrets.compare_digest(pwd.encode(), expected_pass)
                    ):
                        return await call_next(request)
                except Exception:
                    pass
            return Response(
                status_code=401,
                content="Unauthorized",
                headers={"WWW-Authenticate": 'Basic realm="HomelabSage"'},
            )

    @app.on_event("startup")
    async def _start() -> None:
        nonlocal scheduler
        if cfg.scheduler.enabled:
            scheduler = AsyncIOScheduler(timezone=cfg.scheduler.timezone)
            scheduler.add_job(
                engine.run_once,
                CronTrigger.from_crontab(cfg.scheduler.cron, timezone=cfg.scheduler.timezone),
                id="run_once",
                misfire_grace_time=3600,
            )
            scheduler.start()
            log.info("Scheduler started: cron=%r tz=%s", cfg.scheduler.cron, cfg.scheduler.timezone)

    @app.on_event("shutdown")
    async def _stop() -> None:
        if scheduler:
            scheduler.shutdown(wait=False)
        engine.close()

    # ─── Routes ────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        items = db.list(limit=500)
        # Group by status for the simple summary at the top
        counts = {s.value: 0 for s in UpdateStatus}
        for it in items:
            counts[it.status.value] += 1
        tmpl = env.get_template("index.html")
        return HTMLResponse(tmpl.render(items=items, counts=counts, llm_enabled=engine.llm.is_enabled()))

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

    # ─── Notes editor ──────────────────────────────────────────────

    @app.get("/notes", response_class=HTMLResponse)
    async def notes_list() -> HTMLResponse:
        tmpl = env.get_template("notes_list.html")
        return HTMLResponse(tmpl.render(
            files=editor.list(),
            enabled=editor.enabled,
            notes_dir=str(editor.dir) if editor.dir else "",
        ))

    @app.get("/notes/edit", response_class=HTMLResponse)
    async def notes_edit(filename: str | None = None) -> HTMLResponse:
        """Editor view. If `filename` is None or new, show empty editor."""
        tmpl = env.get_template("notes_edit.html")
        if not editor.enabled:
            raise HTTPException(400, "notes_dir not configured")
        is_new = not filename
        content = ""
        if filename:
            try:
                content = editor.read(filename)
            except (PermissionError, ValueError, FileNotFoundError) as e:
                raise HTTPException(404, str(e)) from e
        return HTMLResponse(tmpl.render(filename=filename or "", content=content, is_new=is_new))

    @app.post("/notes/save")
    async def notes_save(
        filename: str = Form(...), content: str = Form(""),
    ) -> RedirectResponse:
        try:
            editor.write(filename, content)
        except (PermissionError, ValueError) as e:
            raise HTTPException(400, str(e)) from e
        return RedirectResponse("/notes", status_code=303)

    @app.post("/notes/delete")
    async def notes_delete(filename: str = Form(...)) -> RedirectResponse:
        try:
            editor.delete(filename)
        except (PermissionError, ValueError) as e:
            raise HTTPException(400, str(e)) from e
        return RedirectResponse("/notes", status_code=303)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    return app


def run_web(cfg: Config) -> None:
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.web.host, port=cfg.web.port, log_level="info")
