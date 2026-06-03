"""`homelabsage chronicle` — narrative timeline of homelab changes.

Reads the last N days of update rows from SQLite and prints a Markdown
logbook (applied / dismissed / breaking / hold / severity-jump events).
When `notes.notes_dir` is set, also writes `notes/chronicle.md` so the
curator picks it up as context on the next pass.
"""

from __future__ import annotations

from pathlib import Path

import typer

from ..chronicle import render_markdown, run_chronicle
from ..config import load_config
from ..db import Database
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging


@app.command()
def chronicle(
    config: Path = CONFIG_OPT,
    days: int = typer.Option(
        30, "--days",
        help="Lookback window in days. Default 30 (one month).",
    ),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Print a narrative chronicle of homelab changes in the lookback window."""
    setup_logging(verbose)
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    try:
        chronicle_obj, notes_path = run_chronicle(
            db, cfg.notes.notes_dir or None, days=days,
        )
    finally:
        db.close()
    console.print(render_markdown(chronicle_obj))
    if notes_path:
        console.print(f"\n[dim]Also written to[/dim] {notes_path}")
