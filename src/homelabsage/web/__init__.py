"""Web UI + background scheduler.

The package layout is one route group per file — adding a new page should mean
"drop a `register_<thing>_routes` next to the others", not "merge into a
200-line module". Server-rendered HTML, no SPA. HTMX is allowed when we get
to interactive forms (interview mode, settings UI).

Public surface (kept stable for `from homelabsage.web import …`):

    create_app(cfg)   → FastAPI app, wired up but not started
    run_web(cfg)      → uvicorn.run(create_app(cfg))
"""

from __future__ import annotations

import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import __version__
from ..config import Config
from ..db import Database
from ..engine import Engine
from ..notes import NotesEditor
from .auth import attach_basic_auth
from .csrf import attach_csrf_guard
from .lifecycle import register_lifecycle
from .routes_health import register_health_routes
from .routes_llm_profiles import register_llm_profiles_routes
from .routes_notes import register_notes_routes
from .routes_settings import register_settings_routes
from .routes_settings_html import register_settings_html_routes
from .routes_settings_test import register_settings_test_routes
from .routes_updates import register_updates_routes
from .routes_wizard import register_wizard_routes

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def create_app(cfg: Config, cfg_path: Path | None = None) -> FastAPI:
    """Wire up the FastAPI app.

    `cfg_path`, when supplied, enables the settings PATCH / DELETE endpoints
    by telling them where to write the `config.user.yaml` overlay. Tests can
    omit it and still exercise everything except those write endpoints.
    """
    app = FastAPI(title="HomelabSage", version=__version__, docs_url=None, redoc_url=None)
    db = Database(cfg.storage.database_path)
    engine = Engine(cfg, db, cfg_path=cfg_path)
    editor = NotesEditor(cfg.notes.notes_dir or None)
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
    )

    # Middleware ordering in Starlette: registered first runs INNERMOST,
    # last runs OUTERMOST (it's `reversed(user_middleware)` in the build
    # step). We want CSRF to run BEFORE auth on the inbound path so a
    # cross-origin attacker gets a 403 regardless of authentication
    # state — that means CSRF must be the OUTERMOST middleware, which
    # means we register it AFTER auth.
    if cfg.web.auth.enabled and cfg.web.auth.password:
        attach_basic_auth(app, cfg.web.auth)
    attach_csrf_guard(app)

    register_lifecycle(app, cfg, engine)
    register_updates_routes(app, db, engine, env)
    register_notes_routes(app, editor, env)
    register_settings_routes(app, cfg, cfg_path)
    # LLM profiles owns `/settings/llm/profiles*` and the dashboard switch
    # `/llm/active`. Register BEFORE the schema-driven settings HTML routes,
    # which include a catch-all `/settings/{block:path}` that would otherwise
    # match `/settings/llm/profiles` and 404 (not a known block).
    register_llm_profiles_routes(app, cfg, cfg_path, env)
    # The connection-test routes use sibling URLs of the catch-all
    # `/settings/{block:path}` from settings_html, so they MUST be registered
    # FIRST — otherwise the catch-all wins and the test endpoints 404.
    register_settings_test_routes(app, cfg, cfg_path, env)
    register_settings_html_routes(app, cfg, cfg_path, env)
    register_wizard_routes(app, cfg, cfg_path, env)
    register_health_routes(app)

    # Static assets (HTMX, favicon, future CSS sprites). Mounted last so
    # route handlers win on `/`-rooted paths.
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # Browsers request `/favicon.ico` automatically before reading the
    # `<link rel=icon>` tag. Serve the PNG asset for both URLs to keep the
    # logs clean and the icon visible on first paint.
    favicon_png = STATIC_DIR / "favicon.png"
    if favicon_png.exists():
        @app.get("/favicon.ico", include_in_schema=False)
        async def favicon() -> FileResponse:
            return FileResponse(favicon_png, media_type="image/png")

    return app


def run_web(cfg: Config, cfg_path: Path | None = None) -> None:
    app = create_app(cfg, cfg_path=cfg_path)
    uvicorn.run(app, host=cfg.web.host, port=cfg.web.port, log_level="info")


__all__ = ["create_app", "run_web"]
