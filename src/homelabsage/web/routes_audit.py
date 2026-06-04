"""Proactive auditor — `/audit` HTML + `/api/audit` JSON + SSE stream.

Read-only: the report is rebuilt on every request from the DB + live
parity probe. Cheap (no outbound HTTP), no caching needed for a homelab
scan tempo.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
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

    @app.get("/api/audit/history")
    async def audit_history_api(limit: int = 50, offset: int = 0) -> JSONResponse:
        """Paginated list of past audit snapshots, newest first.

        Returns the compact per-snapshot summary (severity/category
        counts + finding_count) — never the full findings list. Use
        `/api/audit/diff` to see what changed since the most recent
        snapshot.
        """
        from ..audit_history import list_history
        cap_limit = max(1, min(limit, 200))
        cap_offset = max(0, min(offset, 10_000))
        notes_dir = cfg.curator.output_dir or cfg.notes.notes_dir
        rows = await asyncio.to_thread(
            list_history, notes_dir, limit=cap_limit, offset=cap_offset,
        )
        return JSONResponse({"count": len(rows), "items": rows})

    @app.get("/api/audit/diff")
    async def audit_diff_api() -> JSONResponse:
        """Diff the current findings against the last persisted snapshot.

        Reads `<notes_dir>/audit_history.jsonl` and returns
        `{previous_snapshot, new, resolved}`. Useful for
        "what's changed since last scan?" without polling the full
        report.
        """
        from ..audit_history import diff_against_latest
        report = build_report(cfg, db)
        notes_dir = cfg.curator.output_dir or cfg.notes.notes_dir
        diff = await asyncio.to_thread(
            diff_against_latest, notes_dir, report.to_json(),
        )
        return JSONResponse(diff)

    @app.get("/api/audit/stream")
    async def audit_stream() -> StreamingResponse:
        """Server-Sent Events stream of audit findings.

        Useful when a UI wants to render findings as they arrive
        instead of waiting for the full JSON blob. Pure SSE: each
        finding becomes one `event: finding` frame, with a final
        `event: done` frame carrying the summary.

        The build is one shot — we don't poll for new findings.
        Clients reconnect to refresh.
        """
        report = await asyncio.to_thread(build_report, cfg, db)

        async def _gen() -> AsyncIterator[bytes]:
            # SSE comment line keeps proxies from buffering before
            # the first event arrives.
            yield b": ping\n\n"
            for f in report.findings:
                payload = json.dumps({
                    "severity": f.severity,
                    "category": f.category,
                    "title": f.title,
                    "detail": f.detail,
                    "source_kind": f.source_kind,
                    "source_ref": f.source_ref,
                    "cite": f.cite,
                })
                yield f"event: finding\ndata: {payload}\n\n".encode()
            summary = json.dumps({
                "generated_at": report.generated_at,
                "healthy": report.healthy,
                "counts_by_severity": report.counts_by_severity,
                "counts_by_category": report.counts_by_category,
            })
            yield f"event: done\ndata: {summary}\n\n".encode()

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",   # disable nginx proxy buffering
            },
        )
