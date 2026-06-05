"""Proactive auditor — `/audit` HTML + `/api/audit` JSON + SSE stream.

Read-only: the report is rebuilt on every request from the DB + live
parity probe. Cheap (no outbound HTTP), no caching needed for a homelab
scan tempo.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException
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

    @app.get("/api/audit/by-category")
    async def audit_by_category_api(
        category: str = "",
        limit: int = 100,
    ) -> JSONResponse:
        """Return only the audit findings in `category`.

        Designed for a dashboard that drilled down from
        `/api/audit/categories` into one tag (`cve`, `compose_lint`,
        `disk_pressure`, etc.). Returns the full finding payload
        so the dashboard can render details — capped by `limit`.
        """
        if not category.strip():
            return JSONResponse({"count": 0, "items": [],
                                 "category": ""})
        cap = max(1, min(limit, 500))
        report = await asyncio.to_thread(build_report, cfg, db)
        matches = [
            f for f in report.findings
            if f.category == category
        ][:cap]
        return JSONResponse({
            "category": category,
            "count": len(matches),
            "items": [
                {
                    "severity": f.severity,
                    "category": f.category,
                    "title": f.title,
                    "detail": f.detail,
                    "source_kind": f.source_kind,
                    "source_ref": f.source_ref,
                    "cite": f.cite,
                }
                for f in matches
            ],
        })

    @app.get("/api/audit/categories")
    async def audit_categories_api() -> JSONResponse:
        """Compact category + severity histogram (no findings list).

        Mirrors the MCP `audit_categories` tool — right shape for a
        Homepage / Homarr stripe widget that polls every few minutes
        and shouldn't pull megabytes of finding detail.
        """
        report = await asyncio.to_thread(build_report, cfg, db)
        return JSONResponse({
            "counts_by_category": report.counts_by_category,
            "counts_by_severity": report.counts_by_severity,
            "total": len(report.findings),
            "healthy": report.healthy,
        })

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

    @app.get("/api/audit/history.jsonl")
    async def audit_history_raw() -> StreamingResponse:
        """Raw `audit_history.jsonl` download.

        Streams the file unmodified — caller is responsible for
        parsing one JSON object per line. Useful for backup / offline
        analysis. Returns an empty body when the file is missing.
        """
        from pathlib import Path

        from ..audit_history import FILE_NAME
        notes_dir = cfg.curator.output_dir or cfg.notes.notes_dir
        path = Path(notes_dir).expanduser() / FILE_NAME if notes_dir else None

        async def _gen() -> AsyncIterator[bytes]:
            if path is None or not path.is_file():
                return
            with path.open("rb") as fh:
                # 64 KB chunks — bounded RAM regardless of file size.
                while True:
                    chunk = fh.read(64 * 1024)
                    if not chunk:
                        return
                    yield chunk

        return StreamingResponse(
            _gen(),
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-cache",
                "Content-Disposition": (
                    'attachment; filename="audit_history.jsonl"'
                ),
            },
        )

    @app.get("/api/audit/mutes")
    async def audit_mutes_list(include_expired: bool = False) -> JSONResponse:
        """List the active audit-finding mutes."""
        rows = await asyncio.to_thread(
            db.list_audit_mutes, include_expired=include_expired,
        )
        return JSONResponse({"count": len(rows), "items": rows})

    @app.post("/api/audit/mutes")
    async def audit_mutes_add(payload: dict) -> JSONResponse:
        """Add or refresh a mute.

        Body: `{"category", "source_kind", "source_ref",
        "expires_at"?, "reason"?}`. `expires_at` is ISO 8601 UTC;
        empty/null means permanent. Returns `{ok, fingerprint}`.
        """
        for field in ("category", "source_kind", "source_ref"):
            val = payload.get(field)
            if not isinstance(val, str) or not val.strip():
                raise HTTPException(
                    400, f"{field} is required (non-empty string)",
                )
        expires = payload.get("expires_at")
        if expires is not None and not isinstance(expires, str):
            raise HTTPException(400, "expires_at must be a string or null")
        if expires:
            from .._time import parse_iso
            try:
                parse_iso(expires)
            except ValueError:
                raise HTTPException(  # noqa: B904
                    400, "expires_at must be ISO 8601",
                )
        reason = payload.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise HTTPException(400, "reason must be a string or null")
        await asyncio.to_thread(
            db.add_audit_mute,
            category=payload["category"],
            source_kind=payload["source_kind"],
            source_ref=payload["source_ref"],
            expires_at=expires or None,
            reason=reason or None,
        )
        return JSONResponse({
            "ok": True,
            "fingerprint": {
                "category": payload["category"],
                "source_kind": payload["source_kind"],
                "source_ref": payload["source_ref"],
            },
            "expires_at": expires or None,
        })

    @app.delete("/api/audit/mutes/expired")
    async def audit_mutes_purge_expired() -> JSONResponse:
        """Drop every mute row whose `expires_at` is in the past."""
        n = await asyncio.to_thread(db.purge_expired_audit_mutes)
        return JSONResponse({"ok": True, "purged": n})

    @app.delete("/api/audit/mutes")
    async def audit_mutes_remove(payload: dict) -> JSONResponse:
        """Drop a mute by fingerprint."""
        for field in ("category", "source_kind", "source_ref"):
            val = payload.get(field)
            if not isinstance(val, str) or not val.strip():
                raise HTTPException(400, f"{field} is required")
        ok = await asyncio.to_thread(
            db.remove_audit_mute,
            category=payload["category"],
            source_kind=payload["source_kind"],
            source_ref=payload["source_ref"],
        )
        if not ok:
            raise HTTPException(404, "no mute matches that fingerprint")
        return JSONResponse({"ok": True})

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
