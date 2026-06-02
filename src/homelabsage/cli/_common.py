"""Shared CLI primitives: logger setup, the Typer app singleton, common options.

Every command module imports `app`, `console` and the option singletons from
here. Keeps the registration order deterministic — see `__init__.py` for the
import chain that drives it.
"""

from __future__ import annotations

import logging
from pathlib import Path

import typer
from rich.console import Console

log = logging.getLogger(__name__)

# The Typer app object. Subcommand modules attach via `@app.command()`.
app: typer.Typer = typer.Typer(
    add_completion=False,
    help="HomelabSage — AI-powered homelab analyzer.",
)
console = Console()


def setup_logging(verbose: bool) -> None:
    """Initialise logging once at command entry. Idempotent."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


# ─── shared CLI option singletons ───────────────────────────────────────
#
# Module-level so they avoid `typer.Option(...)` calls inside function defaults
# — ruff B008 rejects the inline form, and reusing the same singleton keeps
# the help text consistent across commands that accept the same flag.

CONFIG_OPT = typer.Option(
    "config.yaml", "--config", "-c",
    help="Path to config.yaml.", show_default=True,
)

VERBOSE_OPT = typer.Option(
    False, "--verbose", "-v",
    help="Debug logging.",
)

CURATE_TARGET_OPT = typer.Option(
    None, "--target", "-t",
    help="Container name to curate. Repeat for several.",
)

EXPORT_OUTPUT_OPT = typer.Option(
    Path("-"), "--output", "-o",
    help="Output path. `-` writes to stdout.",
)

EXPORT_REDACT_OPT = typer.Option(
    True, "--redact/--no-redact",
    help=(
        "Strip IPs, hostnames, and credentials. Default: on. "
        "Turn off only when piping into a tool you fully trust."
    ),
)

DIGEST_CHANNEL_OPT = typer.Option(
    None, "--channel",
    help=(
        "Restrict delivery to specific channels (telegram|discord|ntfy|"
        "gotify). Repeat for several. Defaults to every enabled channel."
    ),
)
