"""`homelabsage list` — show stored updates in a table."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from ..config import load_config
from ..db import Database
from ..models import UpdateStatus
from ._common import CONFIG_OPT, app, console


@app.command("list")
def list_cmd(
    config: Path = CONFIG_OPT,
    source: str | None = typer.Option(None, help="Filter by plugin id (e.g. docker)."),
    status: str | None = typer.Option(None, help="Filter by status."),
    limit: int = typer.Option(50, help="Max rows."),
) -> None:
    """Show stored updates."""
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    status_enum = UpdateStatus(status) if status else None
    items = db.list(status=status_enum, source=source, limit=limit)
    db.close()
    if not items:
        console.print("[dim]No updates stored yet.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Source")
    table.add_column("Subject")
    table.add_column("Current → New")
    table.add_column("Severity")
    table.add_column("Status")
    table.add_column("Summary", overflow="fold", max_width=60)
    for it in items:
        u = it.update
        a = it.analysis
        table.add_row(
            u.source,
            u.subject,
            f"{u.current_version} → {u.new_version}",
            a.severity.value if a else "-",
            it.status.value,
            (a.summary if a else "")[:140],
        )
    console.print(table)
