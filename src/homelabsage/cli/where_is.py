"""`homelabsage where-is <name>` — find where a service is declared.

Walks the compose graph + greps the raw YAML for a line number. Useful
when the user knows a container name but not which compose file owns
it (Dockge stacks balloon fast).
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from ..compose import build_graph
from ..config import load_config
from ..where_is import find
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging

_JSON_OPT = typer.Option(
    False, "--json",
    help="Emit JSON instead of the colour summary.",
)


@app.command(name="where-is")
def where_is_cmd(
    name: str = typer.Argument(..., help="Service name, container_name, or substring."),
    config: Path = CONFIG_OPT,
    json_out: bool = _JSON_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Locate `<name>` in compose files."""
    setup_logging(verbose)
    cfg = load_config(config)
    roots = list(cfg.sources.docker.compose_scan_paths or [])
    if not roots:
        console.print(
            "[yellow]sources.docker.compose_scan_paths is empty.[/yellow]"
        )
        raise SystemExit(0)
    graph = build_graph(roots)
    hits = find(graph, name)
    if json_out:
        for h in hits:
            print(json.dumps(h.to_context()))
        return
    if not hits:
        console.print(f"[dim]No match for {name!r}.[/dim]")
        return
    console.print(f"[bold]{len(hits)} match(es) for {name!r}:[/bold]\n")
    for h in hits:
        loc = f"{h.file}:{h.line}" if h.line else h.file
        cname = (
            f" [dim](container: {h.container_name})[/dim]"
            if h.container_name and h.container_name != h.name else ""
        )
        console.print(
            f"[cyan]{h.name}[/cyan]{cname} "
            f"[dim]→[/dim] [bold]{loc}[/bold]"
        )
        if h.image:
            console.print(f"  [dim]image:[/dim] {h.image}")
        if h.context:
            for ln in h.context.splitlines():
                console.print(f"  [dim]│[/dim] {ln}")
        console.print()
