"""`homelabsage digest` — build and send the weekly rollup."""

from __future__ import annotations

from pathlib import Path

import typer

from ..config import load_config
from ._common import (
    CONFIG_OPT,
    DIGEST_CHANNEL_OPT,
    VERBOSE_OPT,
    app,
    console,
    setup_logging,
)


@app.command()
def digest(
    config: Path = CONFIG_OPT,
    days: int = typer.Option(
        0, "--days",
        help="Override digest.lookback_days for this run (0 = use config).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Print the digest body to stdout instead of sending it.",
    ),
    channel: list[str] = DIGEST_CHANNEL_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Build and send the weekly rollup. Useful for one-shot manual sends."""
    setup_logging(verbose)
    cfg = load_config(config)
    from ..digest import run_digest_blocking

    lookback = days if days > 0 else cfg.digest.lookback_days
    body, results, notes_path = run_digest_blocking(
        cfg,
        days=lookback,
        dry_run=dry_run,
        channels=channel or None,
    )
    if dry_run:
        console.print(body)
        return
    if notes_path:
        console.print(f"[dim]Wrote digest to[/dim] {notes_path}")
    if not results:
        console.print(
            "[yellow]No channels enabled — set `outputs.<name>.enabled: true` "
            "or pass `--channel <name>` to deliver this digest.[/yellow]"
        )
        return
    for ch, status in results.items():
        colour = "green" if status == "sent" else "yellow"
        console.print(f"[{colour}]{ch:<10}[/{colour}] {status}")
