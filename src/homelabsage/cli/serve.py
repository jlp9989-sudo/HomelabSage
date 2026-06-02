"""`homelabsage serve` (long-running web UI) and `homelabsage version`."""

from __future__ import annotations

from pathlib import Path

from .. import __version__
from ..config import load_config
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging


@app.command()
def serve(config: Path = CONFIG_OPT, verbose: bool = VERBOSE_OPT) -> None:
    """Run the web UI + background scheduler (long-running)."""
    setup_logging(verbose)
    cfg = load_config(config)
    # Lazy import to keep CLI startup fast.
    from ..web import run_web

    # Pass the config path through so the settings API knows where to write
    # the user overlay (`config.user.yaml`).
    run_web(cfg, cfg_path=config)


@app.command()
def version() -> None:
    """Print the installed HomelabSage version."""
    console.print(__version__)
