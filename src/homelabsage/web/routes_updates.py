"""Routes for the analyzed-updates dashboard.

  GET  /                  list of analyzed updates, severity-coloured
  POST /run               kick off a scan in the background
  POST /updates/<id>/status   apply / dismiss a row
  GET  /api/updates       JSON API for programmatic consumers
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, Form, HTTPException, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment

from ..db import Database
from ..engine import Engine
from ..models import UpdateStatus
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
    # `update_id` is `source:subject:new_version`; subject/version reach us
    # from external pushers via /api/inbox, so escape before it lands in an
    # HTML attribute (the index template autoescapes, these fragments don't).
    update_id = _html_escape(update_id)
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
    update_id = _html_escape(update_id)
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
    update_id = _html_escape(update_id)
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
        safe_id = _html_escape(update_id)
        return HTMLResponse(
            f'<button class="{cls}" '
            f'hx-post="/updates/{safe_id}/star/toggle" '
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

