"""`homelabsage log-anomaly` — one-shot log-anomaly scan.

Useful for cron-driven probes that don't want to wait for the next
scheduled scan. Prints one line per flagged container; exits 1 if any
fired (so it can drive Uptime Kuma-style status pipelines).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..config import load_config
from ..db import Database
from ..log_anomaly import run_log_anomaly_scan
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging


@app.command(name="log-anomaly")
def log_anomaly(
    config: Path = CONFIG_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """One-shot proactive log-anomaly scan."""
    setup_logging(verbose)
    cfg = load_config(config)
    if not cfg.log_anomaly.enabled:
        console.print(
            "[yellow]log_anomaly.enabled is false — nothing to do.[/yellow]"
        )
        raise SystemExit(0)
    db = Database(cfg.storage.database_path)
    try:
        signals = asyncio.run(run_log_anomaly_scan(
            cfg.log_anomaly, db,
            socket=cfg.sources.docker.socket,
        ))
    finally:
        db.close()
    if not signals:
        console.print("[green]No anomalies detected.[/green]")
        raise SystemExit(0)
    for s in signals:
        console.print(
            f"[red]{s.container_name}[/red] "
            f"error/min={s.error_rate:.2f} "
            f"baseline={s.baseline_mean:.2f}±{s.baseline_std:.2f} "
            f"σ={s.sigma:.1f}"
        )
    raise SystemExit(1)
