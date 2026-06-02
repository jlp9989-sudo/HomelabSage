"""`homelabsage csi <container>` — post-mortem assistant for a single container."""

from __future__ import annotations

from pathlib import Path

import typer

from ..config import load_config
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging


@app.command()
def csi(
    container: str = typer.Argument(..., help="Container name (as in `docker ps`)."),
    config: Path = CONFIG_OPT,
    evidence_only: bool = typer.Option(
        False, "--evidence-only",
        help="Skip the LLM call — print only the gathered logs/notes/update.",
    ),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Post-mortem assistant for a single container.

    Pulls the last detected update + container logs since that timestamp,
    filters to ERROR/WARN/FATAL lines, cross-references your notes, and
    asks the LLM to diagnose. Use --evidence-only to skip the LLM and just
    print the raw signals.
    """
    setup_logging(verbose)
    cfg = load_config(config)
    from ..csi import _evidence_only_report, gather_evidence, run_csi_blocking

    if evidence_only:
        evidence = gather_evidence(cfg, container)
        console.print(_evidence_only_report(evidence))
        return
    report, _ = run_csi_blocking(cfg, container)
    console.print(report)
