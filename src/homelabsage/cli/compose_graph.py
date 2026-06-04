"""`homelabsage compose-graph` — render the compose dependency graph.

Outputs a Mermaid `flowchart` diagram of every parsed compose service
plus its `depends_on` edges. The Mermaid form is paste-ready into any
Markdown viewer (GitHub, Notion, Obsidian, Homepage widgets) and
useful for "show me what depends on the postgres container before I
nuke it".

Edges:
  - `A --> B` means A depends on B (so B should start first).
  - Service nodes are grouped per compose project for clarity.

By default writes to stdout; `--out path.md` writes to a file with a
Markdown fence so the viewer renders it directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from ..compose import build_graph
from ..config import load_config
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging

_MERMAID_HEADER = "flowchart LR"

_OUT_OPT = typer.Option(
    Path(""), "--out",
    help="Write a Markdown-fenced file (default stdout).",
)


def _node_id(service_name: str) -> str:
    """Mermaid node ids accept alnum + underscore; sanitise the rest."""
    out = []
    for ch in service_name:
        if ch.isalnum() or ch == "_":
            out.append(ch)
        else:
            out.append("_")
    sanitised = "".join(out)
    # Mermaid rejects ids starting with a digit — prefix when needed.
    if sanitised and sanitised[0].isdigit():
        sanitised = "n_" + sanitised
    return sanitised or "n_unknown"


def render_mermaid(services: dict, dependents: dict) -> str:
    """Build the mermaid body from the parsed services map.

    `services` is the DependencyGraph.services dict; `dependents` is
    the reverse-dep map (kept private on the dataclass, but the
    caller passes it in for clarity here).
    """
    lines: list[str] = [_MERMAID_HEADER]
    # Group services by project for subgraph blocks.
    by_project: dict[str, list] = {}
    for svc in services.values():
        proj = svc.project or "(no-project)"
        by_project.setdefault(proj, []).append(svc)

    for proj, svcs in sorted(by_project.items()):
        proj_id = _node_id(proj.replace("/", "_"))
        lines.append(f'  subgraph {proj_id}["{proj}"]')
        for svc in sorted(svcs, key=lambda s: s.name):
            sid = _node_id(svc.name)
            label = svc.name
            if svc.container_name and svc.container_name != svc.name:
                label = f"{svc.name}\\n[{svc.container_name}]"
            lines.append(f'    {sid}["{label}"]')
        lines.append("  end")

    # Edges (depends_on)
    edges: list[tuple[str, str]] = []
    for svc in services.values():
        sid = _node_id(svc.name)
        for dep in svc.depends_on:
            edges.append((sid, _node_id(dep)))
    for src, dst in sorted(set(edges)):
        lines.append(f"  {src} --> {dst}")

    # Tag dependents annotation as a comment header so a reader can
    # spot reverse-deps quickly. Mermaid `%%` comments survive most
    # renderers.
    if dependents:
        lines.append(
            f"  %% {sum(len(s) for s in dependents.values())} dependency edges"
        )
    return "\n".join(lines) + "\n"


@app.command(name="compose-graph")
def compose_graph_cmd(
    config: Path = CONFIG_OPT,
    out: Path = _OUT_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Render compose dependency graph as Mermaid."""
    setup_logging(verbose)
    cfg = load_config(config)
    roots = list(cfg.sources.docker.compose_scan_paths or [])
    if not roots:
        console.print(
            "[yellow]sources.docker.compose_scan_paths is empty — "
            "nothing to render.[/yellow]"
        )
        raise SystemExit(0)

    graph = build_graph(roots)
    # The dependents map is private on the dataclass; pass it via the
    # internal attr (we own this module's contract).
    dependents = getattr(graph, "_dependents", {})
    body = render_mermaid(graph.services, dependents)

    if str(out):
        fenced = f"```mermaid\n{body}```\n"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(fenced)
        console.print(f"[green]Wrote {out}[/green]")
        raise SystemExit(0)

    sys.stdout.write(body)
    sys.stdout.flush()
    raise SystemExit(0)
