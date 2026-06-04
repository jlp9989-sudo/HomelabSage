"""`homelabsage doctor` — bundled diagnostic of every probe HomelabSage owns.

Runs every active probe in sequence and prints a single coloured
summary:

  * LLM endpoint health (cfg.llm)
  * TLS certs (cfg.tls_check.urls)
  * DNS resolution (hosts derived from TLS URLs)
  * Disk pressure (cfg.disk_pressure.paths)
  * Compose overrides + env-file perms (from compose_scan_paths)
  * Audit-finding count by severity

Exit code:
  0 = all green
  1 = at least one finding of severity ≥ medium
  2 = LLM endpoint unreachable

This is the "what's broken right now?" 30-second answer. Designed
to be wired into a cron / Kuma push so a single boolean tells
the user whether the homelab needs eyes-on.
"""

from __future__ import annotations

from pathlib import Path

import typer

from ..config import get_active_llm_config, load_config
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging


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

    bad = 0
    llm_unreachable = False

    console.print("[bold]HomelabSage doctor[/bold]\n")

    # ── LLM health ─────────────────────────────────────────────────
    if not skip_llm:
        from ..llm_health import probe as probe_llm
        llm_cfg = get_active_llm_config(cfg)
        verdict = probe_llm(
            llm_cfg.endpoint, api_key=llm_cfg.api_key, timeout=5.0,
        )
        if verdict.ok:
            console.print(
                f"[green]✓ LLM[/green] {llm_cfg.endpoint} — {verdict.reason}"
            )
        else:
            console.print(
                f"[red]✗ LLM[/red] {llm_cfg.endpoint} — {verdict.reason}"
            )
            llm_unreachable = True
            bad += 1
    else:
        console.print("[dim]· LLM check skipped[/dim]")

    # ── TLS ────────────────────────────────────────────────────────
    if cfg.tls_check.urls:
        from ..tls_check import check_urls
        for r in check_urls(
            list(cfg.tls_check.urls),
            warn_days=cfg.tls_check.warn_days,
        ):
            days = r.days_until_expiry
            days_str = "?" if days is None else str(days)
            if r.severity in ("high", "critical"):
                console.print(
                    f"[red]✗ TLS {r.url}[/red] — {r.severity} — "
                    f"days={days_str} — {r.reason}"
                )
                bad += 1
            elif r.severity == "medium":
                console.print(
                    f"[yellow]· TLS {r.url}[/yellow] — medium — "
                    f"days={days_str}"
                )
                bad += 1
            else:
                console.print(
                    f"[green]✓ TLS {r.url}[/green] — days={days_str}"
                )
    else:
        console.print("[dim]· TLS check: no URLs configured[/dim]")

    # ── DNS ────────────────────────────────────────────────────────
    if cfg.tls_check.urls:
        from urllib.parse import urlparse

        from ..dns_check import check_hostnames
        hosts = []
        for raw in cfg.tls_check.urls:
            p = urlparse(raw if "://" in raw else "https://" + raw)
            if p.hostname:
                hosts.append(p.hostname)
        dns_findings = check_hostnames(hosts) if hosts else []
        if not dns_findings:
            console.print(
                f"[green]✓ DNS[/green] all {len(hosts)} host(s) resolve"
            )
        else:
            for f in dns_findings:
                console.print(
                    f"[red]✗ DNS {f.hostname}[/red] — {f.error}"
                )
                bad += 1

    # ── Disk pressure ─────────────────────────────────────────────
    if cfg.disk_pressure.enabled and cfg.disk_pressure.paths:
        from ..disk_pressure import evaluate as eval_disk
        disk_findings = eval_disk(list(cfg.disk_pressure.paths))
        if not disk_findings:
            console.print("[green]✓ Disk[/green] all paths healthy")
        else:
            for d in disk_findings:
                colour = {
                    "critical": "red", "high": "red",
                    "medium": "yellow",
                }.get(d.severity, "white")
                free_gib = d.free_bytes / (1024 ** 3)
                console.print(
                    f"[{colour}]· Disk {d.path}[/{colour}] — {d.severity} — "
                    f"{free_gib:.1f} GiB free ({d.percent_free:.1f}%)"
                )
                bad += 1
    else:
        console.print("[dim]· Disk pressure: not configured[/dim]")

    # ── Compose overrides + env perms ─────────────────────────────
    if cfg.sources.docker.compose_scan_paths:
        from ..compose_override import scan as scan_overrides
        from ..env_perms import scan as scan_env_perms
        overrides = scan_overrides(
            list(cfg.sources.docker.compose_scan_paths),
        )
        if overrides:
            console.print(
                f"[cyan]i Compose overrides[/cyan] — {len(overrides)} file(s)"
            )
        else:
            console.print("[green]✓ Compose overrides[/green] none")
        env_findings = scan_env_perms(
            list(cfg.sources.docker.compose_scan_paths),
        )
        if env_findings:
            for e in env_findings:
                colour = {
                    "critical": "red", "high": "red",
                    "medium": "yellow",
                }.get(e.severity, "white")
                console.print(
                    f"[{colour}]· env-perms {e.path}[/{colour}] — "
                    f"{e.mode_octal} ({e.reason})"
                )
                bad += 1
        else:
            console.print("[green]✓ env file permissions[/green]")

    # ── Audit summary ─────────────────────────────────────────────
    try:
        from ..audit import build_report
        from ..db import Database
        db = Database(cfg.storage.database_path)
        report = build_report(cfg, db)
        sev = report.counts_by_severity or {}
        crit = sev.get("critical", 0)
        high = sev.get("high", 0)
        med = sev.get("medium", 0)
        info = sev.get("info", 0)
        console.print(
            f"\n[bold]Audit:[/bold] "
            f"[red]{crit} critical[/red] · [red]{high} high[/red] · "
            f"[yellow]{med} medium[/yellow] · [cyan]{info} info[/cyan]"
        )
        if crit or high:
            bad += crit + high
    except Exception as e:
        console.print(f"[red]✗ Audit[/red] couldn't build report: {e}")
        bad += 1

    # ── Verdict ───────────────────────────────────────────────────
    console.print()
    if llm_unreachable:
        console.print("[bold red]Verdict: LLM unreachable.[/bold red]")
        raise SystemExit(2)
    if bad == 0:
        console.print("[bold green]Verdict: healthy ✓[/bold green]")
        raise SystemExit(0)
    console.print(f"[bold red]Verdict: {bad} actionable finding(s).[/bold red]")
    raise SystemExit(1)
