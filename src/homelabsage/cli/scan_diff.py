"""`homelabsage scan-diff old.csv new.csv` — compare two history dumps."""

from __future__ import annotations

from pathlib import Path

import typer

from ..scan_diff import diff, render_markdown
from ._common import VERBOSE_OPT, app, console, setup_logging

_OLD_ARG = typer.Argument(..., exists=True, readable=True)
_NEW_ARG = typer.Argument(..., exists=True, readable=True)


@app.command(name="scan-diff")
def scan_diff(
    old: Path = _OLD_ARG,
    new: Path = _NEW_ARG,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Diff two `homelabsage history -o file.csv` dumps."""
    setup_logging(verbose)
    d = diff(
        old.read_text(encoding="utf-8"),
        new.read_text(encoding="utf-8"),
    )
    console.print(render_markdown(d))
    raise SystemExit(0 if d.empty() else 1)
