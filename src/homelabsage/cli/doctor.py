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
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging


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


@app.command(name="doctor")
def doctor_cmd(
    config: Path = CONFIG_OPT,
    skip_llm: bool = typer.Option(
        False, "--skip-llm",
        help="Don't probe the LLM endpoint (useful when offline).",
    ),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """One-shot diagnostic of every active probe."""
    setup_logging(verbose)
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    report = build_report(cfg, db, skip_llm=skip_llm)

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
        raise SystemExit(2)
    if report["healthy"]:
        console.print("[bold green]Verdict: healthy ✓[/bold green]")
        raise SystemExit(0)
    console.print(
        f"[bold red]Verdict: {bad} actionable finding(s).[/bold red]"
    )
    raise SystemExit(1)
