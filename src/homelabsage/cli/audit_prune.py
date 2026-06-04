"""`homelabsage audit-prune` — truncate audit_history.jsonl.

The history file is append-only by design (no rewrite during
scans, so concurrent writers can't race), but daily scans over
months produce a multi-MB file. This CLI lets the user keep just
the last N rows on a manual or cron schedule.
"""

from __future__ import annotations

from pathlib import Path

import typer

from ..audit_history import prune
from ..config import load_config
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging

_KEEP_OPT = typer.Option(
    100, "--keep",
    help="Keep this many newest rows (default 100). 0 truncates the file.",
)


@app.command(name="audit-prune")
def audit_prune_cmd(
    config: Path = CONFIG_OPT,
    keep: int = _KEEP_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Drop older audit history rows, keeping the most recent N."""
    setup_logging(verbose)
    cfg = load_config(config)
    notes_dir = cfg.notes.notes_dir
    if not notes_dir:
        console.print(
            "[yellow]notes.notes_dir is empty — nothing to prune.[/yellow]"
        )
        raise SystemExit(0)
    dropped = prune(notes_dir, keep_last=keep)
    if dropped == 0:
        console.print("[dim]Nothing to prune.[/dim]")
    else:
        console.print(
            f"[green]Pruned {dropped} row(s); kept the {keep} newest.[/green]"
        )
    raise SystemExit(0)
