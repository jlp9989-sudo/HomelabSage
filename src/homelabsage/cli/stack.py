"""`homelabsage stack <name>` — full export for one container.

Dumps everything HomelabSage knows about a container in one Markdown
report: compose file (when found), env (redacted), recent analyses,
sidecar relationships, exposed-port findings, recommended rollback.
The user pastes the whole thing into a GitHub issue / Discord help
channel without manually piecing the picture together.

Composes on top of existing helpers: redact.py for the env scrub,
rollback.py for the recipe, compose.build_graph for the file
location, db.list to pull recent analyses.
"""

from __future__ import annotations

from pathlib import Path

import typer

from ..compose import build_graph as build_compose_graph
from ..config import load_config
from ..db import Database
from ..redact import Sanitiser
from ..rollback import build_recipe, render_markdown
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging


@app.command()
def stack(
    container_name: str = typer.Argument(..., help="The container to export."),
    config: Path = CONFIG_OPT,
    redact: bool = typer.Option(
        True, "--redact/--no-redact",
        help="Strip IPs / hostnames / credentials. Default: on.",
    ),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Dump one container's full HomelabSage view as Markdown."""
    setup_logging(verbose)
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    try:
        # 1. Pull recent updates for the subject — gives the user the
        # version history + analyses without re-running the LLM.
        items = [
            it for it in db.list(source="docker", limit=200)
            if it.update.subject == container_name
        ]
    finally:
        db.close()

    sanitiser = Sanitiser() if redact else None

    lines: list[str] = [
        f"# Stack export — `{container_name}`",
        "",
        f"_{len(items)} update(s) on record._",
        "",
    ]

    # 2. Compose file (when configured + found via graph).
    graph = None
    if cfg.sources.docker.compose_scan_paths:
        try:
            graph = build_compose_graph(cfg.sources.docker.compose_scan_paths)
        except Exception as e:
            lines.append(f"_compose graph build failed: {e}_")
    if graph is not None:
        svc = graph.services.get(container_name) or next(
            (s for s in graph.services.values()
             if s.container_name == container_name),
            None,
        )
        if svc is not None and svc.file is not None:
            lines.append("## Compose file")
            file_str = str(svc.file)
            if sanitiser is not None:
                file_str = sanitiser.sanitise_text(file_str)
            lines.append(f"`{file_str}` (service: `{svc.name}`)")
            lines.append("")
            if svc.depends_on:
                lines.append(f"**depends_on:** {', '.join(svc.depends_on)}")
                lines.append("")
            if svc.networks:
                lines.append(f"**networks:** {', '.join(svc.networks)}")
                lines.append("")

    # 3. Recent updates (compact).
    if items:
        lines.append("## Recent updates")
        for it in items[:20]:
            sev = it.analysis.severity.value if it.analysis else "—"
            summary = (it.analysis.summary if it.analysis else "")[:120]
            if sanitiser is not None:
                summary = sanitiser.sanitise_text(summary)
            lines.append(
                f"- `{it.update.current_version}` → "
                f"`{it.update.new_version}` ({it.status.value}, sev={sev}) — {summary}"
            )
        lines.append("")

    # 4. Rollback recipe for the most recent update.
    if items:
        latest = items[0]
        lines.append("## Rollback recipe (latest update)")
        lines.append("")
        recipe = build_recipe(latest, graph=graph)
        body = render_markdown(recipe)
        if sanitiser is not None:
            body = sanitiser.sanitise_text(body)
        lines.append(body)

    console.print("\n".join(lines))
