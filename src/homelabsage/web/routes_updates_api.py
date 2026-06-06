"""JSON API routes for the analyzed-updates dashboard.

Split out of `routes_updates.py` in v0.11.3 to keep the HTML surface
file focused on rendering. Endpoints registered here:

  GET    /api/updates                          all updates as JSON
  GET    /api/updates/{id}/note                read note
  POST   /api/updates/{id}/note                set note
  POST   /api/updates/{id}/star                set bookmark
  GET    /api/updates/snoozed                  currently snoozed
  DELETE /api/updates/snoozed                  clear all snoozes
  GET    /api/updates/recurring-failures       repeating fails
  POST   /api/updates/{id}/snooze              set/clear snooze
  GET    /api/updates/starred                  bookmarked rows
  GET    /api/updates/search?q=                substring search
  POST   /api/updates/bulk                     bulk apply/dismiss
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, HTTPException

from ..db import Database
from ..engine import Engine
from ..models import UpdateStatus


def register_updates_api_routes(
    app: FastAPI, db: Database, engine: Engine,
) -> None:
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

    @app.get("/api/updates/snoozed")
    async def api_list_snoozed(limit: int = 200) -> dict:
        """Return currently-snoozed updates (snooze_until in the future)."""
        cap = max(1, min(limit, 500))
        rows = await asyncio.to_thread(db.list_snoozed, limit=cap)
        return {"count": len(rows), "items": rows}

    @app.delete("/api/updates/snoozed")
    async def api_clear_all_snoozes() -> dict:
        """Clear `snooze_until` on every snoozed update. Returns count."""
        cleared = await asyncio.to_thread(db.clear_all_snoozes)
        return {"ok": True, "cleared": cleared}

    @app.get("/api/updates/recurring-failures")
    async def api_recurring_failures(
        min_count: int = 2, limit: int = 100,
    ) -> dict:
        """Updates that have flipped to FAILED at least `min_count` times."""
        m = max(1, min_count)
        cap = max(1, min(limit, 500))
        rows = await asyncio.to_thread(
            db.list_recurring_failures, min_count=m, limit=cap,
        )
        return {"count": len(rows), "items": rows}

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
        cap = max(1, min(limit, 200))
        items = await asyncio.to_thread(db.search, q, limit=cap)
        return {
            "query": q,
            "count": len(items),
            "items": [it.model_dump(mode="json") for it in items],
        }

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
        ids = payload.get("ids")
        status_raw = payload.get("status")
        if not isinstance(ids, list) or not isinstance(status_raw, str) or not status_raw:
            raise HTTPException(
                400, "payload must be {ids: [<string>...], status: '...'}",
            )
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
