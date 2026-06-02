"""`homelabsage notify-pending` — replay missed push notifications.

Catch-up flow for analyses that weren't pushed in real time, e.g. because
the parity gate was active. Idempotent in spirit; not idempotent on the
delivery side (Telegram dedupes nothing) — keep the window small.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path

import typer

from ..config import load_config
from ..db import Database
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging

log = logging.getLogger(__name__)


@app.command("notify-pending")
def notify_pending(
    config: Path = CONFIG_OPT,
    hours: int = typer.Option(
        48, "--hours",
        help=(
            "Look back this many hours for analyzed items whose push "
            "notification may have been skipped (parity gate, transient "
            "outage). Default 48h."
        ),
    ),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Replay push notifications for recently analyzed items.

    Useful after a parity check finishes and you want the notifications
    that were skipped to fire now. The push outputs themselves are
    idempotent in spirit but NOT in practice (Telegram has no message
    de-duplication) — re-running this with a small window is safe; running
    with `--hours 720` will spam a month of notifications, so don't.
    """
    setup_logging(verbose)
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    try:
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        # build_outputs already filters by enabled flag
        from ..engine import build_outputs
        outputs = [o for o in build_outputs(cfg, db) if o.is_push]
        if not outputs:
            console.print("[yellow]No push outputs enabled — nothing to flush.[/yellow]")
            return

        items = [
            it for it in db.list(limit=500)
            if it.analyzed_at and it.analyzed_at >= cutoff and it.analysis
        ]
        if not items:
            console.print(f"[dim]No analyzed items in the last {hours}h.[/dim]")
            return

        async def _flush():
            for it in items:
                for out in outputs:
                    try:
                        await out.send(it)
                    except Exception as e:
                        log.exception("notify-pending: %s on %s: %s", out.id, it.id, e)

        asyncio.run(_flush())
        console.print(
            f"[green]Replayed[/green] {len(items)} item(s) across "
            f"{len(outputs)} push output(s)."
        )
    finally:
        db.close()
