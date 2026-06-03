"""`homelabsage audit` — print the proactive auditor's report.

Reads the same data the `/audit` web page renders. Writes a copy to
`<notes_dir>/audit.md` when notes are configured, so the curator picks
it up on the next pass.
"""

from __future__ import annotations

from pathlib import Path

from ..audit import render_markdown, run_audit
from ..config import load_config
from ..db import Database
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging


@app.command()
def audit(
    config: Path = CONFIG_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Print the proactive auditor's homelab health report."""
    setup_logging(verbose)
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    try:
        report, notes_path = run_audit(cfg, db)
    finally:
        db.close()
    console.print(render_markdown(report))
    if notes_path:
        console.print(f"\n[dim]Also written to[/dim] {notes_path}")
