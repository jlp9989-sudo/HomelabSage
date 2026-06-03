"""`homelabsage health-check` — drain the queued post-update probes.

Normally the engine calls `run_due_checks` once per scan; this CLI
exists for manual catch-up + cron-driven probes that don't want to
wait for the next scan.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..config import load_config
from ..db import Database
from ..health_check import run_due_checks
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging


@app.command(name="health-check")
def health_check(
    config: Path = CONFIG_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Run every queued post-update health probe whose grace window passed."""
    setup_logging(verbose)
    cfg = load_config(config)
    if not cfg.health_check.enabled:
        console.print(
            "[yellow]health_check.enabled is false — nothing to do.[/yellow]"
        )
        raise SystemExit(0)
    db = Database(cfg.storage.database_path)
    try:
        results = asyncio.run(run_due_checks(
            cfg.health_check, db,
            socket=cfg.sources.docker.socket,
        ))
    finally:
        db.close()
    if not results:
        console.print("[green]No probes were due.[/green]")
        raise SystemExit(0)
    failed = 0
    for r in results:
        if r.ok:
            console.print(f"[green]{r.container_name}[/green] — healthy")
        else:
            console.print(
                f"[red]{r.container_name}[/red] — signal={r.signal} "
                f"excerpt={r.excerpt or '(none)'}"
            )
            failed += 1
    raise SystemExit(0 if failed == 0 else 1)
