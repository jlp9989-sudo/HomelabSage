"""`homelabsage audit` — print the proactive auditor's report.

Reads the same data the `/audit` web page renders. Writes a copy to
`<notes_dir>/audit.md` when notes are configured, so the curator picks
it up on the next pass.

`--jsonl` streams one JSON object per finding to stdout — pipeable
into `jq` for filtering. Bypasses the notes write so the JSONL form
stays a side-effect-free read.

`--severity {info,medium,high,critical}` hides anything below the
given floor. Both `--jsonl` and the default Markdown output honour
the filter so a Kuma alarm can `audit --severity high --jsonl |
wc -l` to count high+critical findings without parsing severity strings.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import typer

from ..audit import build_report, render_markdown, run_audit
from ..config import load_config
from ..db import Database
from ..models import Severity, severity_order
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging


def _validate_severity(value: str) -> str:
    if not value:
        return ""
    if value not in {s.value for s in Severity}:
        raise typer.BadParameter(
            f"--severity must be one of {sorted(s.value for s in Severity)} (got {value!r})"
        )
    return value


def _filter_findings(report, floor: str):
    """Return a copy of `report` with findings below `floor` removed.

    Recomputes `counts_by_severity` + `counts_by_category` against
    the surviving findings so downstream renderers stay consistent.
    """
    if not floor:
        return report
    threshold = severity_order(floor)
    kept = [
        f for f in report.findings
        if severity_order(f.severity) >= threshold
    ]
    by_sev: dict[str, int] = {}
    by_cat: dict[str, int] = {}
    for f in kept:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
        by_cat[f.category] = by_cat.get(f.category, 0) + 1
    return replace(
        report,
        findings=kept,
        counts_by_severity=by_sev,
        counts_by_category=by_cat,
        healthy=(not kept),
    )


@app.command()
def audit(
    config: Path = CONFIG_OPT,
    jsonl: bool = typer.Option(
        False, "--jsonl",
        help="Stream one JSON object per finding to stdout (no Markdown).",
    ),
    severity: str = typer.Option(
        "", "--severity",
        help=(
            "Hide findings below this severity floor. "
            "One of info|medium|high|critical."
        ),
        callback=_validate_severity,
    ),
    diff_only: bool = typer.Option(
        False, "--diff-only",
        help=(
            "Show only findings that are NEW vs the latest persisted "
            "snapshot in `<notes_dir>/audit_history.jsonl`. Pure read; "
            "no append. No-op when no previous snapshot exists."
        ),
    ),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Print the proactive auditor's homelab health report."""
    setup_logging(verbose)
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    try:
        if diff_only:
            # Pure read — never appends to history, never writes notes.
            from dataclasses import replace as _replace

            from ..audit_history import _fingerprint, load_latest
            report = build_report(cfg, db)
            notes_dir = cfg.notes.notes_dir
            prev = load_latest(notes_dir) if notes_dir else None
            if prev is not None:
                prev_keys = prev.finding_keys
                kept = [
                    f for f in report.findings
                    if _fingerprint({
                        "category": f.category,
                        "source_kind": f.source_kind,
                        "source_ref": f.source_ref,
                    }) not in prev_keys
                ]
                by_sev: dict[str, int] = {}
                by_cat: dict[str, int] = {}
                for f in kept:
                    by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
                    by_cat[f.category] = by_cat.get(f.category, 0) + 1
                report = _replace(
                    report, findings=kept,
                    counts_by_severity=by_sev,
                    counts_by_category=by_cat,
                    healthy=(not kept),
                )
            report = _filter_findings(report, severity)
            if jsonl:
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
            console.print(render_markdown(report))
            return

        if jsonl:
            # No notes write; raw streaming form for piping into jq.
            report = build_report(cfg, db)
            report = _filter_findings(report, severity)
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
        # Filtering happens AFTER persistence: audit_history.jsonl + the
        # `audit.md` written by `run_audit` stay full-fidelity. The
        # filter only affects what we print to stdout.
        report = _filter_findings(report, severity)
    finally:
        db.close()
    console.print(render_markdown(report))
    if notes_path:
        console.print(f"\n[dim]Also written to[/dim] {notes_path}")
