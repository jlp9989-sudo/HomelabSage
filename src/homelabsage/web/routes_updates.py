"""Routes for the analyzed-updates dashboard.

  GET  /                  list of analyzed updates, severity-coloured
  POST /run               kick off a scan in the background
  POST /updates/<id>/status   apply / dismiss a row
  GET  /api/updates       JSON API for programmatic consumers
"""

from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment

from ..db import Database
from ..engine import Engine
from ..models import AnalyzedUpdate, UpdateStatus
from .routes_wizard import is_wizard_complete


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
    )


def _note_cell_html(update_id: str, note: str) -> str:
    """Render the note cell — the saved value + an `edit` link. Shared by
    the index template's initial render and the HTMX save target so the
    swapped fragment matches the static one byte-for-byte.
    """
    if note:
        body = (
            f'<span class="note-text">{_html_escape(note)}</span> '
            f'<a href="#" class="muted" '
            f'hx-get="/updates/{update_id}/note/edit" '
            f'hx-target="closest .note-cell" hx-swap="outerHTML" '
            f'title="Edit note">edit</a>'
        )
    else:
        body = (
            f'<a href="#" class="muted" '
            f'hx-get="/updates/{update_id}/note/edit" '
            f'hx-target="closest .note-cell" hx-swap="outerHTML">add note</a>'
        )
    return f'<td class="note-cell">{body}</td>'


def _note_edit_form_html(update_id: str, current: str) -> str:
    """Render the inline textarea + save/cancel buttons. Posted to
    `/updates/{id}/note`; the response is `_note_cell_html` again so
    save returns the user to the read view.
    """
    return (
        f'<td class="note-cell">'
        f'<form hx-post="/updates/{update_id}/note" '
        f'hx-target="closest .note-cell" hx-swap="outerHTML" '
        f'style="display:flex; gap:.3rem; flex-direction:column;">'
        f'<textarea name="note" rows="2" '
        f'style="min-height:3rem; font-size:.8rem;" '
        f'autocomplete="off">{_html_escape(current)}</textarea>'
        f'<div style="display:flex; gap:.3rem;">'
        f'<button class="ghost" type="submit" '
        f'style="font-size:.75rem; padding:.15rem .55rem;">save</button>'
        f'<button class="ghost" type="button" '
        f'hx-get="/updates/{update_id}/note/cancel" '
        f'hx-target="closest .note-cell" hx-swap="outerHTML" '
        f'style="font-size:.75rem; padding:.15rem .55rem;">cancel</button>'
        f'</div></form></td>'
    )


def _snooze_cell_html(update_id: str, snooze_until: str | None) -> str:
    """Render the snooze cell — a tiny form group used by the index +
    the `quick_snooze_html` HTMX target. Kept module-level so both
    callers stay in sync without circular helpers.
    """
    if snooze_until:
        # Pretty up the timestamp: keep `YYYY-MM-DD HH:MM` then drop the
        # rest (timezone suffix). The DB always stores ISO 8601 UTC so
        # the split is safe.
        pretty = snooze_until.replace("T", " ")[:16]
        return (
            f'<span class="muted" title="{snooze_until}">💤 {pretty}</span> '
            f'<form method="post" action="/updates/{update_id}/snooze/quick" '
            f'hx-post="/updates/{update_id}/snooze/quick" '
            f'hx-swap="outerHTML" '
            f'hx-target="closest .snooze-cell" style="display:inline">'
            f'<input type="hidden" name="days" value="0">'
            f'<button class="ghost" type="submit" title="Clear snooze">'
            f'clear</button></form>'
        )
    return (
        f'<form method="post" action="/updates/{update_id}/snooze/quick" '
        f'hx-post="/updates/{update_id}/snooze/quick" '
        f'hx-swap="outerHTML" '
        f'hx-target="closest .snooze-cell" style="display:inline">'
        f'<select name="days" onchange="this.form.requestSubmit()" '
        f'aria-label="Snooze duration">'
        f'<option value="">snooze…</option>'
        f'<option value="7">7d</option>'
        f'<option value="14">14d</option>'
        f'<option value="30">30d</option>'
        f'<option value="90">90d</option>'
        f'</select></form> '
        f'<form method="post" action="/updates/{update_id}/snooze/until" '
        f'hx-post="/updates/{update_id}/snooze/until" '
        f'hx-swap="outerHTML" '
        f'hx-target="closest .snooze-cell" style="display:inline">'
        f'<input type="date" name="until" '
        f'aria-label="Snooze until date" '
        f'onchange="this.form.requestSubmit()" '
        f'style="font-size:.75rem; padding:.15rem .3rem; max-width:9rem;">'
        f'</form>'
    )


def register_updates_routes(
    app: FastAPI, db: Database, engine: Engine, env: Environment
) -> None:
    @app.get("/", response_class=HTMLResponse)
    async def index(filter: str = "") -> HTMLResponse:  # noqa: A002 — `filter` shadows builtin but FastAPI uses the param name as the query key
        all_items = db.list(limit=500)
        # Group by status for the simple summary at the top BEFORE filtering
        # — so the count pills always reflect the whole DB regardless of
        # which view the user is on.
        counts = {s.value: 0 for s in UpdateStatus}
        for it in all_items:
            counts[it.status.value] += 1

        # Pre-compute per-update star + snooze hints so the template
        # can render the right button state without per-row DB hits.
        starred_ids: set[str] = {
            it.id for it in db.list_starred(limit=500)
        }
        snoozed_until: dict[str, str] = {
            row["id"]: row["snooze_until"]
            for row in db.list_snoozed(limit=500)
        }
        explained_ids: set[str] = db.list_explained_ids()
        notes_by_id: dict[str, str] = db.list_user_notes()
        # COUNT(*) helpers stay accurate beyond the 500-row list cap.
        starred_count = db.count_starred()
        snoozed_count = db.count_snoozed()

        # Filter views (header pill links). Default `""` = all updates.
        if filter == "starred":
            items = [it for it in all_items if it.id in starred_ids]
        elif filter == "snoozed":
            items = [it for it in all_items if it.id in snoozed_until]
        else:
            items = all_items

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
            not is_wizard_complete(engine._cfg_path) and not all_items
        )
        tmpl = env.get_template("index.html")
        return HTMLResponse(
            tmpl.render(
                items=items, counts=counts,
                starred_ids=starred_ids, snoozed_until=snoozed_until,
                explained_ids=explained_ids, notes_by_id=notes_by_id,
                starred_count=starred_count, snoozed_count=snoozed_count,
                active_filter=filter or "all",
                llm_enabled=engine.llm.is_enabled(),
                llm_profiles=list(fresh.llm_profiles.keys()),
                llm_active=fresh.llm_active,
                show_wizard_banner=show_wizard_banner,
            )
        )

    @app.get("/search", response_class=HTMLResponse)
    async def search_page(q: str = "") -> HTMLResponse:
        """HTML wrapper around the JSON search API.

        Renders the same `index.html` layout filtered to substring matches
        on subject + summary + breaking_changes + user_note. Empty `q`
        shows the search box with no results — bookmark `/search` to
        always land on the search input.
        """
        q_clean = (q or "").strip()
        items = (
            await asyncio.to_thread(db.search, q_clean, limit=200)
            if q_clean else []
        )
        starred_ids: set[str] = set()
        snoozed_until: dict[str, str] = {}
        starred_ids = {it.id for it in db.list_starred(limit=500)}
        for row in db.list_snoozed(limit=500):
            snoozed_until[row["id"]] = row["snooze_until"]
        explained_ids: set[str] = db.list_explained_ids()
        notes_by_id: dict[str, str] = db.list_user_notes()
        counts = {s.value: 0 for s in UpdateStatus}
        for it in items:
            counts[it.status.value] += 1
        starred_count = db.count_starred()
        snoozed_count = db.count_snoozed()
        tmpl = env.get_template("index.html")
        return HTMLResponse(
            tmpl.render(
                items=items, counts=counts,
                starred_ids=starred_ids, snoozed_until=snoozed_until,
                explained_ids=explained_ids, notes_by_id=notes_by_id,
                starred_count=starred_count, snoozed_count=snoozed_count,
                active_filter="all",
                llm_enabled=engine.llm.is_enabled(),
                llm_profiles=list(engine.cfg.llm_profiles.keys()),
                llm_active=engine.cfg.llm_active,
                show_wizard_banner=False,
                search_query=q_clean,
            ),
        )

    @app.get("/api/updates/history.csv")
    async def download_history_csv(limit: int = 10000) -> Response:
        """Stream the full updates table as CSV — same shape as the
        `homelabsage history -o file.csv` CLI command, just one click
        away from the GUI.
        """
        from ..history import dump_to_string
        cap = max(1, min(limit, 100_000))
        body = await asyncio.to_thread(dump_to_string, db, limit=cap)
        return Response(
            body,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    'attachment; filename="homelabsage-history.csv"'
                ),
                "Cache-Control": "no-cache",
            },
        )

    @app.post("/updates/{update_id:path}/note", response_class=HTMLResponse)
    async def set_note_html(
        update_id: str, note: str = Form(""),
    ) -> HTMLResponse:
        """HTMX target — save a free-text note. Returns the rendered
        note-cell so the form swaps in place from "(no note)" / textarea
        to the saved value with an edit button.
        """
        if db.get(update_id) is None:
            raise HTTPException(404, f"no update with id={update_id}")
        clean = (note or "").strip()
        db.set_user_note(update_id, clean)
        return HTMLResponse(_note_cell_html(update_id, clean))

    @app.get("/updates/{update_id:path}/note/edit", response_class=HTMLResponse)
    async def edit_note_html(update_id: str) -> HTMLResponse:
        """HTMX target — swap the note cell to an editable textarea +
        save button. Pre-fills the current value so the user edits in
        place rather than retyping.
        """
        if db.get(update_id) is None:
            raise HTTPException(404, f"no update with id={update_id}")
        current = db.get_user_note(update_id) or ""
        return HTMLResponse(_note_edit_form_html(update_id, current))

    @app.get("/updates/{update_id:path}/note/cancel", response_class=HTMLResponse)
    async def cancel_note_html(update_id: str) -> HTMLResponse:
        """HTMX target — drop the edit form, restore the read-view cell."""
        if db.get(update_id) is None:
            raise HTTPException(404, f"no update with id={update_id}")
        current = db.get_user_note(update_id) or ""
        return HTMLResponse(_note_cell_html(update_id, current))

    @app.post("/updates/{update_id:path}/star/toggle", response_class=HTMLResponse)
    async def toggle_star_html(update_id: str) -> HTMLResponse:
        """HTMX target — flips the star and returns just the new button HTML.

        Separate from the JSON `POST /api/updates/<id>/star` so the form
        doesn't need a JS layer or a redirect. Returns the rendered
        button so HTMX can swap the cell in place.

        Uses the atomic `toggle_starred` so two simultaneous clicks (double
        click, two tabs) can't both read False and both write True. Returns
        404 if the row is missing rather than rendering a happy button for
        an id that doesn't exist.
        """
        new = db.toggle_starred(update_id)
        if new is None:
            raise HTTPException(404, f"no update with id={update_id}")
        icon = "★" if new else "☆"
        cls = "btn" if new else "btn ghost"
        label = "Unstar" if new else "Star"
        return HTMLResponse(
            f'<button class="{cls}" '
            f'hx-post="/updates/{update_id}/star/toggle" '
            f'hx-swap="outerHTML" '
            f'title="Toggle star" aria-label="{label}" '
            f'aria-pressed="{"true" if new else "false"}">{icon}</button>'
        )

    @app.post("/updates/{update_id:path}/snooze/until", response_class=HTMLResponse)
    async def custom_snooze_html(
        update_id: str, until: str = Form(""),
    ) -> HTMLResponse:
        """HTMX target — snooze until a custom date (YYYY-MM-DD).

        Empty `until` clears. Past dates clear (defensive, mirrors quick).
        Invalid dates → 400. Returns the rendered cell.
        """
        from datetime import UTC as _UTC
        from datetime import date as _date
        from datetime import datetime as _dt

        from .._time import utcnow
        if db.get(update_id) is None:
            raise HTTPException(404, f"no update with id={update_id}")
        until_str = until.strip()
        if not until_str:
            db.set_snooze(update_id, None)
            return HTMLResponse(_snooze_cell_html(update_id, None))
        try:
            target = _date.fromisoformat(until_str)
        except ValueError as e:
            raise HTTPException(400, f"until must be YYYY-MM-DD: {e}") from e
        now = utcnow()
        if target <= now.date():
            db.set_snooze(update_id, None)
            return HTMLResponse(_snooze_cell_html(update_id, None))
        iso = _dt.combine(target, _dt.min.time(), tzinfo=_UTC).isoformat()
        db.set_snooze(update_id, iso)
        return HTMLResponse(_snooze_cell_html(update_id, iso))

    @app.post("/updates/{update_id:path}/snooze/quick", response_class=HTMLResponse)
    async def quick_snooze_html(
        update_id: str, days: int = Form(7),
    ) -> HTMLResponse:
        """HTMX target — snooze for N days (default 7) or clear when days≤0.

        Returns the new snooze cell HTML so HTMX can swap it in place.
        404 when the update id is unknown — better than silently rendering
        a snooze button for a row that doesn't exist.
        """
        from datetime import timedelta as _td

        from .._time import utcnow
        if db.get(update_id) is None:
            raise HTTPException(404, f"no update with id={update_id}")
        if days <= 0:
            db.set_snooze(update_id, None)
            return HTMLResponse(_snooze_cell_html(update_id, None))
        until = (utcnow() + _td(days=days)).isoformat()
        db.set_snooze(update_id, until)
        return HTMLResponse(_snooze_cell_html(update_id, until))

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

    @app.post("/api/webhook/github-release")
    async def api_inbox_github_release(request: Request) -> dict:
        """GitHub Release webhook receiver.

        Configure in your repo: Settings → Webhooks → Add webhook,
        URL = `https://<your-host>/api/webhook/github-release`, content
        type `application/json`, events `Releases`.

        Optional shared-secret verification: set
        `GITHUB_RELEASE_WEBHOOK_SECRET` in env and configure the same
        secret in the GitHub UI. When set, requests without a valid
        `X-Hub-Signature-256` are rejected.

        Only `action: released` (not draft / pre-release / edit) is
        accepted to avoid duplicates from staging releases.
        """
        import hashlib
        import hmac
        import os
        from urllib.parse import urlparse

        from ..models import Update as _Update

        # Hard size cap — GitHub bodies are typically <100 KB but
        # release notes can balloon.
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > 1024 * 1024:
                    raise HTTPException(413, "payload exceeds 1 MB cap")
            except ValueError:
                pass

        raw_body = await request.body()
        secret = os.environ.get("GITHUB_RELEASE_WEBHOOK_SECRET", "")
        # The endpoint is auth-bypassed at the app level so GitHub
        # can reach it without Basic Auth — therefore the HMAC IS
        # the auth. Refuse the request when no secret is configured
        # rather than silently accepting unsigned requests.
        if not secret:
            raise HTTPException(
                503,
                "GITHUB_RELEASE_WEBHOOK_SECRET not configured — "
                "set it in the environment to enable this endpoint",
            )
        sig_header = request.headers.get("X-Hub-Signature-256", "")
        if not sig_header.startswith("sha256="):
            raise HTTPException(401, "missing or malformed signature")
        expected = "sha256=" + hmac.new(
            secret.encode(), raw_body, hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, sig_header):
            raise HTTPException(401, "signature mismatch")

        try:
            payload = json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            raise HTTPException(400, "invalid JSON")  # noqa: B904
        if not isinstance(payload, dict):
            raise HTTPException(400, "payload must be a JSON object")

        action = payload.get("action") or ""
        if action != "released":
            return {"ok": True, "skipped": True,
                    "reason": f"action={action} (only 'released' accepted)"}

        release = payload.get("release") or {}
        repo = payload.get("repository") or {}
        if not isinstance(release, dict) or not isinstance(repo, dict):
            raise HTTPException(400, "missing release / repository envelope")

        tag = release.get("tag_name") or ""
        repo_full = repo.get("full_name") or ""
        if not tag or not repo_full:
            raise HTTPException(400, "release.tag_name + repository.full_name required")
        if len(tag) > 100 or len(repo_full) > 200:
            raise HTTPException(400, "tag or repo name too long")

        release_url = release.get("html_url") or ""
        if release_url:
            scheme = urlparse(release_url).scheme.lower()
            if scheme not in ("http", "https"):
                release_url = ""
            if len(release_url) > 2048:
                release_url = ""
        body_raw = release.get("body") or ""
        body_md = body_raw[:65536] if isinstance(body_raw, str) else ""

        upd = _Update(
            source="github-release",
            subject=repo_full,
            current_version="(unknown)",
            new_version=tag,
            release_url=release_url or None,
            release_notes=body_md,
            context={"webhook": True, "event_action": action},
        )
        analyzed = AnalyzedUpdate(update=upd)
        db.upsert(analyzed)
        return {"ok": True, "id": analyzed.id,
                "subject": repo_full, "tag": tag}

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
