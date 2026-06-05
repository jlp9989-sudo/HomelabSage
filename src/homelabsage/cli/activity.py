"""`homelabsage activity` — recent activity tail.

Shows the last N updates by `detected_at`, plus any recent pending-
dispatch entries — the "what's been happening" view for a returning
operator. Single read; no LLM call, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from ..config import load_config
from ..db import Database
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging

_LIMIT_OPT = typer.Option(
    20, "--limit",
    help="Max updates to show (default 20, max 200).",
)
_JSON_OPT = typer.Option(
    False, "--json",
    help="Emit one JSON object per item on stdout (machine-readable).",
)


def _status_colour(status: str) -> str:
    return {
        "new": "cyan",
        "analyzed": "yellow",
        "applied": "green",
        "dismissed": "dim",
        "failed": "red",
    }.get(status, "white")


@app.command(name="activity")
def activity_cmd(
    config: Path = CONFIG_OPT,
    limit: int = _LIMIT_OPT,
    json_out: bool = _JSON_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Tail recent updates (most-recent first)."""
    setup_logging(verbose)
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    cap = max(1, min(limit, 200))
    items = db.list(limit=cap)

    if json_out:
        for it in items:
            row = {
                "id": it.id,
                "source": it.update.source,
                "subject": it.update.subject,
                "current_version": it.update.current_version,
                "new_version": it.update.new_version,
                "status": it.status.value,
                "detected_at": it.detected_at.isoformat(),
                "severity": (
                    it.analysis.severity.value if it.analysis else None
                ),
            }
            print(json.dumps(row))
        return

    if not items:
        console.print("[dim]No updates yet — run `homelabsage check` first.[/dim]")
        return
    console.print(f"[bold]Last {len(items)} update(s)[/bold]\n")
    for it in items:
        colour = _status_colour(it.status.value)
        sev = (
            f" [yellow]{it.analysis.severity.value}[/yellow]"
            if it.analysis else ""
        )
        console.print(
            f"[dim]{it.detected_at.strftime('%Y-%m-%d %H:%M')}[/dim] "
            f"[{colour}]{it.status.value:>9}[/{colour}]{sev} "
            f"[bold]{it.update.subject}[/bold] "
            f"[dim]({it.update.current_version} → {it.update.new_version})[/dim]"
        )
