"""`homelabsage history` — CSV dump of every detected update.

Friendlier than `sqlite3 state.sqlite '.dump updates'` and covers the
common ask (paste into Numbers / Excel / Sheets to see the trend).
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from ..config import load_config
from ..db import Database
from ..history import dump_to_string, write_csv
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging

# Hoisted out of the function default so ruff's B008 doesn't fire on the
# `typer.Option(Path("-"), ...)` form — same trick the other commands use.
_HISTORY_OUTPUT_OPT = typer.Option(
    Path("-"), "--output", "-o",
    help="Output path. `-` writes to stdout.",
)


@app.command()
def history(
    config: Path = CONFIG_OPT,
    output: Path = _HISTORY_OUTPUT_OPT,
    limit: int = typer.Option(
        10000, "--limit",
        help="Max rows to read from the DB (default 10 000).",
    ),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Dump every analyzed update to CSV."""
    setup_logging(verbose)
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    try:
        if str(output) == "-":
            sys.stdout.write(dump_to_string(db, limit=limit))
        else:
            n = write_csv(db, output, limit=limit)
            console.print(f"[green]Wrote {n} rows → {output}[/green]")
    finally:
        db.close()
