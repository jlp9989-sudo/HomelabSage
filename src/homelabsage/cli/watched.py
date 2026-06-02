"""`homelabsage watched` sub-app — manage the watched-repos plugin's list."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from ..config import load_config
from ..db import Database
from ._common import CONFIG_OPT, app, console

watched_app = typer.Typer(
    add_completion=False,
    help="Manage the list of GitHub/Codeberg repos the watched-repos plugin scans.",
    no_args_is_help=True,
)
app.add_typer(watched_app, name="watched")


@watched_app.command("add")
def watched_add(
    repo: str = typer.Argument(..., help="owner/name or codeberg.org/owner/name."),
    nickname: str = typer.Option(
        "", "--nickname", "-n",
        help="Friendly name used in notifications.",
    ),
    version: str = typer.Option(
        "", "--version",
        help=(
            "Seed `current_version`. Leave empty for first-scan to seed from "
            "the latest release (avoids a phantom 0→N update on add)."
        ),
    ),
    config: Path = CONFIG_OPT,
) -> None:
    """Add a repo to the watched list (idempotent — re-adding revives it active)."""
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    try:
        wid = db.add_watched_repo(
            repo,
            nickname=nickname or None,
            current_version=version or None,
        )
        console.print(f"[green]Watching[/green] {repo} (id={wid})")
    finally:
        db.close()


@watched_app.command("list")
def watched_list(
    config: Path = CONFIG_OPT,
    all_: bool = typer.Option(
        False, "--all", help="Include inactive entries.",
    ),
) -> None:
    """List watched repos."""
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    try:
        rows = db.list_watched_repos(active_only=not all_)
        if not rows:
            console.print("[dim]No watched repos.[/dim]")
            return
        table = Table(show_header=True, header_style="bold")
        table.add_column("ID", justify="right")
        table.add_column("Repo")
        table.add_column("Nickname")
        table.add_column("Current version")
        table.add_column("Active")
        for r in rows:
            table.add_row(
                str(r["id"]),
                r["repo"],
                r["nickname"] or "",
                r["current_version"] or "[dim](not seeded)[/dim]",
                "✓" if r["active"] else "·",
            )
        console.print(table)
    finally:
        db.close()


@watched_app.command("toggle")
def watched_toggle(
    watched_id: int = typer.Argument(..., help="ID from `homelabsage watched list`."),
    config: Path = CONFIG_OPT,
) -> None:
    """Flip a watched repo between active and inactive."""
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    try:
        row = db.get_watched_repo(watched_id)
        if row is None:
            console.print(f"[red]No watched repo with id={watched_id}.[/red]")
            raise typer.Exit(code=1)
        new_state = not bool(row["active"])
        db.toggle_watched_repo(watched_id, new_state)
        word = "activated" if new_state else "deactivated"
        console.print(f"[green]{row['repo']}[/green] {word}")
    finally:
        db.close()


@watched_app.command("remove")
def watched_remove(
    watched_id: int = typer.Argument(..., help="ID from `homelabsage watched list`."),
    config: Path = CONFIG_OPT,
) -> None:
    """Drop a watched repo entirely."""
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    try:
        row = db.get_watched_repo(watched_id)
        if row is None:
            console.print(f"[red]No watched repo with id={watched_id}.[/red]")
            raise typer.Exit(code=1)
        db.remove_watched_repo(watched_id)
        console.print(f"[green]Removed[/green] {row['repo']}")
    finally:
        db.close()
