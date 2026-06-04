"""`homelabsage tls-check` — probe configured URLs and print expiry verdicts.

Reads `tls_check.urls` from config. Exits non-zero when any URL has
severity high/critical so the CLI can drive cron/Kuma pipelines.
"""

from __future__ import annotations

from pathlib import Path

import typer

from ..config import load_config
from ..tls_check import check_urls
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging


@app.command(name="tls-check")
def tls_check(
    config: Path = CONFIG_OPT,
    warn_days: int = typer.Option(
        30, "--warn-days",
        help="Severity buckets: ≤ warn_days → medium, ≤ warn_days/3 → high, ≤0 → critical.",
    ),
    timeout: float = typer.Option(
        10.0, "--timeout",
        help="Per-URL connect+TLS handshake timeout (seconds).",
    ),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Probe configured URLs for TLS cert expiry."""
    setup_logging(verbose)
    cfg = load_config(config)
    urls = list(cfg.tls_check.urls or [])
    if not urls:
        console.print(
            "[yellow]tls_check.urls is empty — nothing to probe.[/yellow]"
        )
        raise SystemExit(0)
    results = check_urls(urls, timeout=timeout, warn_days=warn_days)
    bad = 0
    for r in results:
        sev = r.severity
        if sev in ("high", "critical"):
            colour = "red"
            bad += 1
        elif sev == "medium":
            colour = "yellow"
        else:
            colour = "green"
        days = r.days_until_expiry
        days_str = "?" if days is None else str(days)
        console.print(
            f"[{colour}]{r.url}[/{colour}] — {sev} — "
            f"days={days_str} — {r.reason}"
        )
    raise SystemExit(0 if bad == 0 else 1)
