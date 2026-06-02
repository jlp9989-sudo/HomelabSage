"""Dashboard widget endpoints — Homepage and Homarr.

Two flavours of the same data, shaped for the two big homelab
dashboards' "custom API" widgets:

  GET /widget/homepage   → field/label/value shape consumed by gethomepage
  GET /widget/homarr     → simpler key/value JSON consumed by Homarr's
                            custom-api widget

The body is intentionally read-only and unauthenticated separately:
both dashboards expect to scrape an endpoint without juggling Basic
Auth headers. To keep your install secure, mount HomelabSage behind a
reverse proxy that allows the dashboard's IP to hit `/widget/*` and
nothing else.

Numbers shown:
  - pending updates (analyzed but neither applied nor dismissed)
  - critical / high severity counts
  - parity_gate active flag
  - last_scan_at timestamp (most recent detected_at in the DB)
  - pending_dispatches queue size (push notifications waiting on
    the parity gate to clear)
"""

from __future__ import annotations

from datetime import datetime

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ..config import Config
from ..db import Database
from ..models import UpdateStatus
from ..parity import is_parity_running


def _gather_stats(cfg: Config, db: Database) -> dict:
    """Compute the small set of counters both widget shapes use."""
    items = db.list(limit=500)
    counts = {"critical": 0, "high": 0, "medium": 0, "info": 0}
    pending = 0
    last_scan_at: datetime | None = None
    for it in items:
        if it.detected_at and (last_scan_at is None or it.detected_at > last_scan_at):
            last_scan_at = it.detected_at
        if it.status in (UpdateStatus.APPLIED, UpdateStatus.DISMISSED):
            continue
        pending += 1
        if it.analysis:
            counts[it.analysis.severity.value] = counts.get(it.analysis.severity.value, 0) + 1

    parity = False
    parity_reason = ""
    if cfg.parity_gate.enabled:
        state = is_parity_running(mdstat_path=cfg.parity_gate.mdstat_path)
        parity = state.running
        parity_reason = state.reason

    return {
        "pending": pending,
        "critical": counts["critical"],
        "high": counts["high"],
        "medium": counts["medium"],
        "info": counts["info"],
        "parity_active": parity,
        "parity_reason": parity_reason,
        "queued_dispatches": len(db.list_pending_dispatches()),
        "last_scan_at": last_scan_at.isoformat() if last_scan_at else None,
    }


def register_widget_routes(app: FastAPI, cfg: Config, db: Database) -> None:
    @app.get("/widget/homepage")
    async def homepage_widget() -> JSONResponse:
        """gethomepage custom-api shape: a list of `{label, value}` rows.

        See https://gethomepage.dev/widgets/services/customapi/ — the
        widget renders each list entry as a row in the service card.
        """
        s = _gather_stats(cfg, db)
        rows = [
            {"label": "Pending", "value": s["pending"]},
            {"label": "Critical", "value": s["critical"]},
            {"label": "High", "value": s["high"]},
        ]
        if s["queued_dispatches"]:
            rows.append({"label": "Queued (parity)", "value": s["queued_dispatches"]})
        return JSONResponse(rows)

    @app.get("/widget/homarr")
    async def homarr_widget() -> JSONResponse:
        """Homarr `iframe`/`custom-api` widget — flat JSON.

        Homarr's custom-api widget reads top-level keys directly into a
        configurable display. We expose every counter and let the user
        pick which to render.
        """
        return JSONResponse(_gather_stats(cfg, db))
