"""`homelabsage check` — run a single scan + analyze + output cycle."""

from __future__ import annotations

from pathlib import Path

from ..config import load_config
from ..engine import run_blocking
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging


@app.command()
def check(config: Path = CONFIG_OPT, verbose: bool = VERBOSE_OPT) -> None:
    """Run a single scan + analyze + output cycle."""
    setup_logging(verbose)
    cfg = load_config(config)
    stats = run_blocking(cfg, cfg_path=config)
    console.print(f"[green]Done.[/green] {stats}")
