"""`homelabsage audit` — print the proactive auditor's report.

Reads the same data the `/audit` web page renders. Writes a copy to
`<notes_dir>/audit.md` when notes are configured, so the curator picks
it up on the next pass.

`--jsonl` streams one JSON object per finding to stdout — pipeable
into `jq` for filtering. Bypasses the notes write so the JSONL form
stays a side-effect-free read.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from ..audit import build_report, render_markdown, run_audit
from ..config import load_config
from ..db import Database
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging


@app.command()
def audit(
    config: Path = CONFIG_OPT,
    jsonl: bool = typer.Option(
        False, "--jsonl",
        help="Stream one JSON object per finding to stdout (no Markdown).",
    ),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Print the proactive auditor's homelab health report."""
    setup_logging(verbose)
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    try:
        if jsonl:
            # No notes write; raw streaming form for piping into jq.
            report = build_report(cfg, db)
            for f in report.findings:
                sys.stdout.write(json.dumps({
                    "severity": f.severity,
                    "category": f.category,
                    "title": f.title,
                    "detail": f.detail,
                    "source_kind": f.source_kind,
                    "source_ref": f.source_ref,
                    "cite": f.cite,
                }) + "\n")
            sys.stdout.flush()
            return
        report, notes_path = run_audit(cfg, db)
    finally:
        db.close()
    console.print(render_markdown(report))
    if notes_path:
        console.print(f"\n[dim]Also written to[/dim] {notes_path}")
