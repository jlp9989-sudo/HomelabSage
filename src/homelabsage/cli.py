"""HomelabSage — Typer-based CLI.

Commands:
  homelabsage check                Run a single scan cycle and exit.
  homelabsage list  [--source X]   List stored updates.
  homelabsage curate               Generate per-container Markdown notes.
  homelabsage serve                Start the web UI + scheduler.
  homelabsage version              Print version.
"""

from __future__ import annotations

import asyncio
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
CURATE_TARGET_OPT = typer.Option(
    None, "--target", "-t", help="Container name to curate. Repeat for several."
)


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
def curate(
    config: Path = CONFIG_OPT,
    discover: bool = typer.Option(
        False, "--discover", help="Process every running container with a resolvable repo."
    ),
    target: list[str] | None = CURATE_TARGET_OPT,
    limit: int = typer.Option(0, "--limit", help="Cap on containers processed. 0 = no cap."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the would-be note instead of writing it."
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Regenerate even if the note exists with matching digest or no curator footer.",
    ),
    show_prompt: bool = typer.Option(
        False, "--show-prompt", help="Print the rendered prompt for each target and exit."
    ),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Generate one Markdown note per running container.

    Use `--discover` to walk every running container, or `--target NAME` (one
    or more times) to curate specific ones. `--dry-run` prints the proposed
    note without touching the filesystem.
    """
    _setup_logging(verbose)
    if not discover and not (target or []):
        console.print(
            "[red]Either --discover or at least one --target is required.[/red]"
        )
        raise typer.Exit(code=2)

    cfg = load_config(config)
    if not cfg.curator.enabled:
        console.print("[yellow]curator.enabled is false in config — aborting.[/yellow]")
        raise typer.Exit(code=1)

    # Lazy import so the heavy docker SDK only loads for this subcommand.
    from .curator import Curator
    from .llm import LLMClient

    llm = LLMClient(cfg.llm)
    curator = Curator(
        cfg.curator,
        llm,
        cfg.sources.docker,
        notes_dir=cfg.notes.notes_dir,
    )
    try:
        snapshots = curator.discover_targets(
            limit=limit or None,
            only=list(target) if target else None,  # target may be None
        )
        if not snapshots:
            console.print("[dim]No matching containers found.[/dim]")
            return

        if show_prompt:
            for snap in snapshots:
                release_notes = asyncio.run(curator.fetch_release_context(snap.repo))
                examples = curator.load_style_examples()
                rendered = curator.build_prompt(snap, release_notes, examples)
                console.rule(f"[bold]{snap.name}[/bold]")
                console.print(rendered)
            return

        for snap in snapshots:
            result = asyncio.run(curator.curate_one(snap, dry_run=dry_run, force=force))
            label_color = {
                "written": "green",
                "skipped_same_digest": "dim",
                "skipped_manual": "yellow",
                "skipped_dry_run": "cyan",
                "llm_failed": "red",
            }.get(result.status, "white")
            path_str = str(result.path) if result.path else "(no path)"
            console.print(
                f"[{label_color}]{result.status:<22}[/{label_color}] "
                f"{snap.name:<28} {path_str}"
            )
            if dry_run and result.body:
                console.rule(f"[bold]{snap.name}[/bold]")
                console.print(result.body)
            if result.note:
                console.print(f"  [dim]{result.note}[/dim]")
    finally:
        curator.close()


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
