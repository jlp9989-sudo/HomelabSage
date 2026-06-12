"""`homelabsage doctor` — bundled diagnostic.

Pretty-prints the structured report from `homelabsage.doctor.build_report`.
Same report is served at `GET /api/doctor` and via MCP `doctor` tool —
this CLI is the human-facing colour rendering.

Exit code:
  0 = all green
  1 = at least one finding of severity ≥ medium
  2 = LLM endpoint unreachable
"""

from __future__ import annotations

from pathlib import Path

import typer

from ..config import load_config
from ..db import Database
from ..doctor import build_report
from ..models import Severity, severity_order
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging


def _validate_floor(value: str) -> str:
    if value not in {s.value for s in Severity}:
        raise typer.BadParameter(
            f"--severity-floor must be one of {sorted(s.value for s in Severity)} (got {value!r})"
        )
    return value


def _audit_has_findings_at_or_above(report: dict, floor: str) -> bool:
    """True iff the audit section reports any count at or above `floor`."""
    counts = (
        report.get("sections", {})
        .get("audit", {})
        .get("counts_by_severity", {})
        or {}
    )
    threshold = severity_order(floor)
    for sev, n in counts.items():
        if int(n or 0) <= 0:
            continue
        if severity_order(sev) >= threshold:
            return True
    return False


def _print_section(name: str, section: dict) -> int:
    """Print one section; return count of `bad` findings to add."""
    if section.get("skipped"):
        console.print(
            f"[dim]· {name}: {section.get('reason') or 'skipped'}[/dim]"
        )
        return 0
    if section.get("ok"):
        console.print(f"[green]✓ {name}[/green]")
        return 0
    reason = section.get("reason") or ""
    console.print(f"[red]✗ {name}[/red] {reason}")
    # findings vary per section — render what's available
    bad = 0
    for finding in section.get("findings") or []:
        bad += 1
        sev = finding.get("severity") or "?"
        colour = {"critical": "red", "high": "red",
                  "medium": "yellow"}.get(sev, "white")
        # tls finding shape
        if "url" in finding and "days_until_expiry" in finding:
            console.print(
                f"  [{colour}]TLS {finding['url']}[/{colour}] — "
                f"{sev} — days={finding.get('days_until_expiry')} — "
                f"{finding.get('reason') or ''}"
            )
        elif "hostname" in finding:
            console.print(
                f"  [{colour}]DNS {finding['hostname']}[/{colour}] — "
                f"{finding.get('error', '')}"
            )
        elif "path" in finding and "free_bytes" in finding:
            free_gib = finding["free_bytes"] / (1024 ** 3)
            console.print(
                f"  [{colour}]Disk {finding['path']}[/{colour}] — "
                f"{sev} — {free_gib:.1f} GiB free "
                f"({finding.get('percent_free', 0):.1f}%)"
            )
        else:
            console.print(f"  · {finding}")
    if section.get("bad_count"):
        bad = max(bad, int(section["bad_count"]))
    return bad


def _render_report(report: dict) -> int:
    """Render the doctor report. Return `bad` count for the exit code."""
    console.print("[bold]HomelabSage doctor[/bold]\n")
    bad = 0
    for name in ("llm", "tls", "dns", "disk", "compose", "audit"):
        bad += _print_section(name.upper(), report["sections"][name])

    # compose section is structured (overrides + env_perms) — render
    # overrides as info-level info
    comp = report["sections"]["compose"]
    overrides = comp.get("overrides") or []
    env_perms = comp.get("env_perms") or []
    if overrides:
        console.print(
            f"[cyan]i Compose overrides[/cyan] — {len(overrides)} file(s)"
        )
    for ep in env_perms:
        sev = ep.get("severity") or "?"
        colour = {"critical": "red", "high": "red",
                  "medium": "yellow"}.get(sev, "white")
        console.print(
            f"  [{colour}]env-perms {ep['path']}[/{colour}] — "
            f"{ep['mode_octal']} ({ep['reason']})"
        )

    # Audit count rollup
    audit_section = report["sections"]["audit"]
    sev_counts = audit_section.get("counts_by_severity") or {}
    if sev_counts:
        console.print(
            f"\n[bold]Audit:[/bold] "
            f"[red]{sev_counts.get('critical', 0)} critical[/red] · "
            f"[red]{sev_counts.get('high', 0)} high[/red] · "
            f"[yellow]{sev_counts.get('medium', 0)} medium[/yellow] · "
            f"[cyan]{sev_counts.get('info', 0)} info[/cyan]"
        )

    console.print()
    if report["llm_unreachable"]:
        console.print("[bold red]Verdict: LLM unreachable.[/bold red]")
        return -2
    if report["healthy"]:
        console.print("[bold green]Verdict: healthy ✓[/bold green]")
        return 0
    console.print(
        f"[bold red]Verdict: {bad} actionable finding(s).[/bold red]"
    )
    return bad


@app.command(name="doctor")
def doctor_cmd(
    config: Path = CONFIG_OPT,
    skip_llm: bool = typer.Option(
        False, "--skip-llm",
        help="Don't probe the LLM endpoint (useful when offline).",
    ),
    watch: int = typer.Option(
        0, "--watch",
        help=(
            "Continuous mode — re-run every N seconds. "
            "0 = one-shot (default). Minimum 5 to avoid hammering."
        ),
    ),
    severity_floor: str = typer.Option(
        "medium", "--severity-floor",
        help=(
            "Audit findings below this severity don't flip the exit "
            "code (default `medium`). Probe failures (TLS/DNS/disk/"
            "compose) still flip it regardless."
        ),
        callback=_validate_floor,
    ),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """One-shot (default) or continuous diagnostic of every active probe."""
    setup_logging(verbose)
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)

    if watch <= 0:
        report = build_report(cfg, db, skip_llm=skip_llm)
        outcome = _render_report(report)
        if outcome == -2:
            raise SystemExit(2)
        # Apply severity floor: if `bad` was entirely audit-driven and
        # none of the audit counts meet the floor, treat as healthy.
        if outcome == 0:
            raise SystemExit(0)
        # Outcome > 0: check whether the floor downgrades it to healthy.
        # `bad` may include direct probe failures (TLS/DNS/disk/etc.)
        # which aren't surfaced as audit counts. Be conservative: only
        # downgrade when the audit section is the SOLE contributor.
        audit_section = report.get("sections", {}).get("audit", {})
        if (
            not audit_section.get("ok")
            and audit_section.get("counts_by_severity")
            and not _audit_has_findings_at_or_above(report, severity_floor)
        ):
            # Did any OTHER section fail? Check probe-level ok flags.
            other_failed = False
            for name in ("llm", "tls", "dns", "disk", "compose"):
                sec = report.get("sections", {}).get(name, {})
                if not sec.get("ok") and not sec.get("skipped"):
                    other_failed = True
                    break
            if not other_failed:
                console.print(
                    f"[dim]Audit findings present but below "
                    f"--severity-floor={severity_floor} → exit 0.[/dim]"
                )
                raise SystemExit(0)
        raise SystemExit(1)

    interval = max(5, watch)
    import time
    from datetime import datetime
    console.print(
        f"[dim]Watch mode — every {interval}s. Ctrl-C to stop.[/dim]\n"
    )
    last_outcome: int | None = None
    while True:
        report = build_report(cfg, db, skip_llm=skip_llm)
        # Header per iteration so the user sees the timestamp + delta
        # vs previous iteration's verdict.
        now_s = datetime.now().strftime("%H:%M:%S")
        outcome = _render_report(report)
        if last_outcome is not None and outcome != last_outcome:
            console.print(
                f"[bold yellow]Δ verdict @ {now_s}: "
                f"{last_outcome} → {outcome}[/bold yellow]"
            )
        last_outcome = outcome
        console.print(
            f"[dim]Sleeping {interval}s ({now_s})…[/dim]\n"
        )
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            console.print("[dim]Stopped.[/dim]")
            raise SystemExit(0)  # noqa: B904
