"""`homelabsage analyse <url>` — one-shot URL analysis (sub-case a of the
roadmap's URL analyser: GitHub/Codeberg repo URLs).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from ..config import load_config
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging


@app.command()
def analyse(
    url: str = typer.Argument(..., help="GitHub or Codeberg repo URL."),
    config: Path = CONFIG_OPT,
    version: str = typer.Option(
        "", "--version",
        help="Optional current version you run locally. Improves the analysis.",
    ),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """One-shot analysis of a pasted repo URL. Uses your configured LLM + notes."""
    setup_logging(verbose)
    cfg = load_config(config)
    from ..analyse_url import analyse_repo_url, parse_repo_url

    repo = parse_repo_url(url)
    if repo is None:
        console.print(
            f"[red]Could not parse a GitHub/Codeberg repo URL from {url!r}.[/red]"
        )
        raise typer.Exit(code=2)
    analyzed = asyncio.run(analyse_repo_url(cfg, url, current_version=version))
    if analyzed is None:
        console.print(f"[red]Analysis failed for {repo}.[/red]")
        raise typer.Exit(code=1)
    u = analyzed.update
    a = analyzed.analysis
    console.print(f"[bold]{u.subject}[/bold]  {u.current_version} → {u.new_version}")
    if u.release_url:
        console.print(f"[dim]{u.release_url}[/dim]")
    if a is None:
        console.print("[yellow]LLM analysis unavailable — printed Update only.[/yellow]")
        return
    sev_colour = {
        "critical": "red", "high": "yellow",
        "medium": "cyan", "info": "blue",
    }.get(a.severity.value, "white")
    console.print(
        f"\n[{sev_colour}]{a.severity.value.upper()}[/{sev_colour}] — {a.summary}\n"
    )
    if a.breaking_changes:
        console.print("[bold]Breaking changes:[/bold]")
        for bc in a.breaking_changes:
            console.print(f"  • {bc}")
    if a.new_features_relevant:
        console.print("\n[bold]New features:[/bold]")
        for nf in a.new_features_relevant:
            console.print(f"  • {nf}")
    if a.recommended_action:
        console.print(f"\n[bold]Recommended action:[/bold] {a.recommended_action}")
