"""Routes for the analyzed-updates dashboard.

  GET  /                  list of analyzed updates, severity-coloured
  POST /run               kick off a scan in the background
  POST /updates/<id>/status   apply / dismiss a row
  GET  /api/updates       JSON API for programmatic consumers
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment

from ..db import Database
from ..engine import Engine
from ..models import AnalyzedUpdate, UpdateStatus
from .routes_wizard import is_wizard_complete


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
        # Show the "start the wizard" banner only on a fresh install: no
        # wizard marker yet AND no scan results to show. We deliberately
        # don't redirect — a banner is recoverable, a redirect surprises
        # the user and breaks bookmarks.
        show_wizard_banner = (
            not is_wizard_complete(engine._cfg_path) and not items
        )
        tmpl = env.get_template("index.html")
        return HTMLResponse(
            tmpl.render(
                items=items, counts=counts,
                llm_enabled=engine.llm.is_enabled(),
                llm_profiles=list(fresh.llm_profiles.keys()),
                llm_active=fresh.llm_active,
                show_wizard_banner=show_wizard_banner,
            )
        )

    @app.post("/run")
    async def trigger_run() -> RedirectResponse:
        asyncio.create_task(engine.run_once())
        return RedirectResponse("/", status_code=303)

    @app.post("/updates/{update_id:path}/status")
    async def set_status(
        update_id: str,
        status: str = Form(...),
        ack: str = Form(""),
    ) -> RedirectResponse:
        new_status = UpdateStatus(status)
        # Pre-flight gate: if the user is APPLYING and the analysis carries
        # breaking_changes, force a confirmation page. The gate is opt-in
        # via `web.preflight_gate` so single-user installs can keep the
        # one-click flow if they prefer.
        if (
            new_status == UpdateStatus.APPLIED
            and getattr(engine.cfg.web, "preflight_gate", False)
            and ack != "yes"
        ):
            it = db.get(update_id)
            if it and it.analysis and it.analysis.breaking_changes:
                return RedirectResponse(
                    f"/updates/{update_id}/preflight", status_code=303,
                )
        db.set_status(update_id, new_status)
        # Queue a post-update health probe when applying. Best-effort:
        # missing the container_name (non-docker source) silently skips.
        if new_status == UpdateStatus.APPLIED and engine.cfg.health_check.enabled:
            it = db.get(update_id)
            if it and it.update.source == "docker":
                from ..health_check import queue_for_update
                queue_for_update(
                    db, update_id=update_id,
                    container_name=it.update.subject,
                    cfg=engine.cfg.health_check,
                )
        return RedirectResponse("/", status_code=303)

    @app.get("/updates/{update_id:path}/preflight", response_class=HTMLResponse)
    async def preflight(update_id: str) -> HTMLResponse:
        """Confirmation wall shown when applying an update with breaking
        changes. Renders the breaking_changes + recommended_action and
        requires an explicit ack POST before the status flips."""
        it = db.get(update_id)
        if it is None:
            return HTMLResponse("Not found", status_code=404)
        tmpl = env.get_template("preflight.html")
        return HTMLResponse(tmpl.render(item=it))

    @app.get("/api/updates")
    async def api_updates() -> list[dict]:
        return [it.model_dump(mode="json") for it in db.list(limit=500)]

    @app.post("/api/updates/{update_id:path}/note")
    async def api_set_user_note(update_id: str, payload: dict) -> dict:
        """Attach a free-text note. Body: {"note": "..."} (empty clears)."""
        note = payload.get("note")
        if note is not None and not isinstance(note, str):
            raise HTTPException(400, "note must be a string or null")
        ok = db.set_user_note(update_id, note or "")
        if not ok:
            raise HTTPException(404, f"no update with id={update_id}")
        return {"ok": True, "update_id": update_id,
                "note": db.get_user_note(update_id) or ""}

    @app.get("/api/updates/{update_id:path}/note")
    async def api_get_user_note(update_id: str) -> dict:
        if db.get(update_id) is None:
            raise HTTPException(404, f"no update with id={update_id}")
        return {"update_id": update_id, "note": db.get_user_note(update_id) or ""}

    @app.post("/api/updates/{update_id:path}/star")
    async def api_set_star(update_id: str, payload: dict) -> dict:
        """Toggle the bookmark flag. Body: {"starred": true|false}."""
        starred = payload.get("starred")
        if not isinstance(starred, bool):
            raise HTTPException(400, "starred must be a boolean")
        if not db.set_starred(update_id, starred):
            raise HTTPException(404, f"no update with id={update_id}")
        return {"ok": True, "update_id": update_id,
                "starred": db.is_starred(update_id)}

    @app.post("/api/updates/{update_id:path}/snooze")
    async def api_set_snooze(update_id: str, payload: dict) -> dict:
        """Set or clear the snooze timestamp.

        Body: `{"snooze_until": "2026-06-10T12:00:00Z"}` to snooze, or
        `{"snooze_until": null}` to clear. ISO 8601 strings only.
        """
        raw = payload.get("snooze_until")
        if raw is not None and not isinstance(raw, str):
            raise HTTPException(400, "snooze_until must be a string or null")
        snooze = raw or None
        if snooze:
            # Sanity-check the timestamp by parsing it.
            from .._time import parse_iso
            try:
                parse_iso(snooze)
            except ValueError:
                raise HTTPException(  # noqa: B904
                    400, "snooze_until must be ISO 8601 (e.g. 2026-06-10T12:00:00Z)",
                )
        if not db.set_snooze(update_id, snooze):
            raise HTTPException(404, f"no update with id={update_id}")
        return {"ok": True, "update_id": update_id,
                "snooze_until": db.get_snooze(update_id)}

    @app.get("/api/updates/starred")
    async def api_list_starred(limit: int = 200) -> dict:
        """Return only the bookmarked updates, newest first."""
        import asyncio
        cap = max(1, min(limit, 500))
        items = await asyncio.to_thread(db.list_starred, limit=cap)
        return {
            "count": len(items),
            "items": [it.model_dump(mode="json") for it in items],
        }

    @app.get("/api/updates/search")
    async def api_search(q: str = "", limit: int = 50) -> dict:
        """Substring search across subject + summary + breaking_changes
        + user_note. Empty `q` returns `{count: 0, items: []}`.

        FastAPI validates `limit` as int (bad input → 422); we clamp
        to [1, 200] after validation. Search runs in a worker thread
        because SQLite LIKE on `analysis_json` is a full-table scan
        and we don't want to block the event loop.
        """
        import asyncio
        cap = max(1, min(limit, 200))
        items = await asyncio.to_thread(db.search, q, limit=cap)
        return {
            "query": q,
            "count": len(items),
            "items": [it.model_dump(mode="json") for it in items],
        }

    @app.post("/api/inbox/{source}")
    async def api_inbox(source: str, request: Request) -> dict:
        """Webhook receiver — external systems POST update events here.

        Accepts a flexible payload. Required: `subject`, `new_version`.
        Optional: `current_version` (defaults to "(unknown)"),
        `release_url` (must be http(s):// and ≤2048 chars),
        `release_notes` (≤64 KB), `context` (dict).

        Source string becomes `Update.source`. Restricted to
        `[a-z0-9_-]{1,32}` so a malicious caller can't pollute the
        dashboard with display-control characters.

        Body size: capped at 1 MB. `release_notes` capped at 64 KB
        before persistence so a single big POST can't bloat the DB.
        """
        import re as _re
        from urllib.parse import urlparse

        if not _re.fullmatch(r"[a-z0-9_-]{1,32}", source):
            raise HTTPException(
                400, "source must match [a-z0-9_-]{1,32}",
            )
        # Hard request-body cap. Reject before parsing so a 100 MB POST
        # can't fill memory just to be rejected as "payload too large".
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > 1024 * 1024:
                    raise HTTPException(413, "payload exceeds 1 MB cap")
            except ValueError:
                pass
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(400, "payload must be a JSON object")
        subject = payload.get("subject")
        new_version = payload.get("new_version")
        if not isinstance(subject, str) or not subject.strip():
            raise HTTPException(400, "subject is required")
        if not isinstance(new_version, str) or not new_version.strip():
            raise HTTPException(400, "new_version is required")
        # subject + version: cap to prevent unbounded growth + control
        # chars that would break UI/Notion rendering.
        if len(subject) > 200:
            raise HTTPException(400, "subject too long (max 200)")
        if len(new_version) > 100:
            raise HTTPException(400, "new_version too long (max 100)")
        current_version = payload.get("current_version") or "(unknown)"
        if not isinstance(current_version, str):
            raise HTTPException(400, "current_version must be a string")
        if len(current_version) > 100:
            raise HTTPException(400, "current_version too long (max 100)")
        # release_url: only http(s); reject javascript: / data: shells.
        release_url = payload.get("release_url") or None
        if release_url is not None:
            if not isinstance(release_url, str):
                raise HTTPException(400, "release_url must be a string")
            if len(release_url) > 2048:
                raise HTTPException(400, "release_url too long (max 2048)")
            scheme = urlparse(release_url).scheme.lower()
            if scheme not in ("http", "https"):
                raise HTTPException(
                    400, "release_url scheme must be http or https",
                )
        release_notes = payload.get("release_notes")
        if release_notes is not None and not isinstance(release_notes, str):
            raise HTTPException(400, "release_notes must be a string")
        if isinstance(release_notes, str):
            release_notes = release_notes[:65536]  # 64 KB hard cap
        ctx = payload.get("context") or {}
        if not isinstance(ctx, dict):
            raise HTTPException(400, "context must be an object")
        from ..models import Update as _Update
        upd = _Update(
            source=source,
            subject=subject.strip(),
            current_version=current_version,
            new_version=new_version.strip(),
            release_url=release_url,
            release_notes=release_notes,
            context=ctx,
        )
        analyzed = AnalyzedUpdate(update=upd)
        db.upsert(analyzed)
        return {"ok": True, "id": analyzed.id, "source": source}

    @app.post("/api/updates/bulk")
    async def api_updates_bulk(payload: dict) -> dict:
        """Bulk apply / dismiss many updates at once.

        Payload: {"ids": [...], "status": "applied" | "dismissed" | "failed"}.
        Unknown ids are skipped silently (returned in `not_found`); the
        valid ones get the same per-id treatment as `POST /updates/<id>/status`
        including the post-update health-check queue when applicable.

        Returns counts by outcome so the caller can render "applied 3,
        dismissed 1, 2 not found". Pre-flight gate IS NOT applied here:
        bulk action implies the user already reviewed the list.
        """
        # Validate the envelope before touching anything — non-list `ids`
        # used to silently iterate string characters when callers passed
        # `"ids": "abc"`. Now any non-list shape is a 400.
        ids = payload.get("ids")
        status_raw = payload.get("status")
        if not isinstance(ids, list) or not isinstance(status_raw, str) or not status_raw:
            raise HTTPException(
                400, "payload must be {ids: [<string>...], status: '...'}",
            )
        # Soft cap so a 10k-id POST can't block the event loop with
        # synchronous SQLite writes.
        BULK_MAX = 500
        if len(ids) > BULK_MAX:
            raise HTTPException(
                400, f"too many ids ({len(ids)}); cap is {BULK_MAX}",
            )
        try:
            new_status = UpdateStatus(status_raw)
        except ValueError as e:
            raise HTTPException(400, f"unknown status: {status_raw}") from e

        # Run the SQLite writes off the event loop — 500 sync
        # `set_status` + optional `queue_for_update` calls would
        # otherwise stall every other request for the duration.
        import asyncio

        def _bulk_apply() -> tuple[int, list[str]]:
            applied = 0
            not_found: list[str] = []
            for uid in ids:
                if not isinstance(uid, str):
                    continue
                item = db.get(uid)
                if item is None:
                    not_found.append(uid)
                    continue
                db.set_status(uid, new_status)
                applied += 1
                if (
                    new_status == UpdateStatus.APPLIED
                    and engine.cfg.health_check.enabled
                    and item.update.source == "docker"
                ):
                    from ..health_check import queue_for_update
                    queue_for_update(
                        db, update_id=uid,
                        container_name=item.update.subject,
                        cfg=engine.cfg.health_check,
                    )
            return applied, not_found

        applied, not_found = await asyncio.to_thread(_bulk_apply)
        return {
            "status": new_status.value,
            "applied": applied,
            "not_found": not_found,
            "not_found_count": len(not_found),
        }
