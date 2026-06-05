"""`homelabsage purge` — drop old terminal-state updates.

Removes APPLIED + DISMISSED rows older than N days. Snoozed rows
are never purged (the snooze itself is a "remind me" signal). For a
homelab that scans daily, a 90-day window typically keeps the table
under a few thousand rows.

`--dry-run` reports the count without modifying anything.
"""

from __future__ import annotations

from pathlib import Path

import typer

from ..config import load_config
from ..db import Database
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging

_DAYS_OPT = typer.Option(
    90, "--days",
    help="Delete rows whose detected_at is older than this many days.",
)
_DRY_OPT = typer.Option(
    False, "--dry-run",
    help="Show the count without deleting.",
)


@app.command(name="purge")
def purge_cmd(
    config: Path = CONFIG_OPT,
    days: int = _DAYS_OPT,
    dry_run: bool = _DRY_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Delete APPLIED + DISMISSED updates older than N days."""
    setup_logging(verbose)
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    n = db.purge_old_updates(older_than_days=days, dry_run=dry_run)
    if dry_run:
        console.print(
            f"[yellow]Would purge {n} row(s) older than {days} days.[/yellow]"
        )
    elif n == 0:
        console.print("[dim]Nothing to purge.[/dim]")
    else:
        console.print(
            f"[green]Purged {n} row(s) older than {days} days.[/green]"
        )
    raise SystemExit(0)
