"""HomelabSage — Typer-based CLI.

Commands:
  homelabsage check                Run a single scan cycle and exit.
  homelabsage list  [--source X]   List stored updates.
  homelabsage serve                Start the web UI + scheduler.
  homelabsage version              Print version.
"""

from __future__ import annotations

import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import load_config
from .db import Database
from .engine import run_blocking
from .models import UpdateStatus

app = typer.Typer(add_completion=False, help="HomelabSage — AI-powered homelab analyzer.")
console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


CONFIG_OPT = typer.Option(
    "config.yaml", "--config", "-c", help="Path to config.yaml.", show_default=True
)
VERBOSE_OPT = typer.Option(False, "--verbose", "-v", help="Debug logging.")


@app.command()
def check(config: Path = CONFIG_OPT, verbose: bool = VERBOSE_OPT) -> None:
    """Run a single scan + analyze + output cycle."""
    _setup_logging(verbose)
    cfg = load_config(config)
    stats = run_blocking(cfg)
    console.print(f"[green]Done.[/green] {stats}")


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


@app.command()
def serve(config: Path = CONFIG_OPT, verbose: bool = VERBOSE_OPT) -> None:
    """Run the web UI + background scheduler (long-running)."""
    _setup_logging(verbose)
    cfg = load_config(config)
    # Lazy import to keep CLI startup fast.
    from .web import run_web

    run_web(cfg)


@app.command()
def version() -> None:
    console.print(__version__)


if __name__ == "__main__":
    app()
