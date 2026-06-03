"""`homelabsage backup-check` — probe configured restic/borg/kopia repos
and print a freshness report.

Same code path the auditor + analyzer use, just rendered for human eyes.
Exits non-zero when any repo is in `high` or `critical` so it can drive
a shell pipeline (Uptime Kuma push-on-success, etc).
"""

from __future__ import annotations

from pathlib import Path

from ..backup_health import BackupHealthReport, probe_repo
from ..config import load_config
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging


@app.command(name="backup-check")
def backup_check(
    config: Path = CONFIG_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Probe every configured backup repo and report staleness."""
    setup_logging(verbose)
    cfg = load_config(config)
    if not cfg.backup_health.enabled:
        console.print(
            "[yellow]backup_health.enabled is false — nothing to probe.[/yellow]"
        )
        raise SystemExit(0)
    if not cfg.backup_health.repos:
        console.print(
            "[yellow]No repos configured under backup_health.repos.[/yellow]"
        )
        raise SystemExit(0)

    results = []
    for repo in cfg.backup_health.repos:
        r = probe_repo(
            name=repo.name, tool=repo.tool, env=repo.env,
            binary=(repo.binary or None),
            warn_after_days=repo.warn_after_days,
            critical_after_days=repo.critical_after_days,
            timeout=float(repo.timeout_seconds),
        )
        results.append(r)

    report = BackupHealthReport(results=results)
    bad = 0
    for r in results:
        if r.ok:
            stale = (
                f"{r.staleness_days}d stale" if r.staleness_days is not None
                else "fresh"
            )
            console.print(
                f"[bold]{r.repo_name}[/bold] ({r.tool}) — "
                f"severity={r.severity} — {stale} — {r.snapshot_count} snapshots"
            )
        else:
            console.print(
                f"[red]{r.repo_name}[/red] ({r.tool}) — FAILED — {r.reason}"
            )
        if r.severity in ("high", "critical"):
            bad += 1

    if not report.healthy():
        raise SystemExit(1)
    raise SystemExit(0)
