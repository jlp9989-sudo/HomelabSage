"""`homelabsage features` — print the version + feature flag map.

Mirror of `GET /api/version` and the MCP `version` tool, surfaced as
a CLI for "what does this binary support?" queries from cron / docs /
ops scripts.
"""

from __future__ import annotations

import json

import typer

from .. import __version__
from ._common import app, console

_JSON_OPT = typer.Option(
    False, "--json",
    help="Emit a single JSON object on stdout (machine-readable).",
)


@app.command(name="features")
def features_cmd(
    json_out: bool = _JSON_OPT,
) -> None:
    """Print the version + feature flag map."""
    parts: list[int] = []
    for p in __version__.split("."):
        try:
            parts.append(int(p.split("-", 1)[0]))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    features_map: dict[str, bool] = {
        "doctor": True,
        "audit_history": True,
        "snooze": True,
        "audit_alerts": True,
    }
    payload: dict = {
        "version": __version__,
        "version_parts": parts[:3],
        "features": features_map,
    }
    if json_out:
        console.print(json.dumps(payload))
        return
    console.print(f"[bold]HomelabSage v{__version__}[/bold]")
    console.print(
        f"[dim]parts:[/dim] {'.'.join(str(p) for p in parts[:3])}"
    )
    console.print("[bold]Features[/bold]:")
    for name, enabled in features_map.items():
        flag = "[green]on[/green]" if enabled else "[red]off[/red]"
        console.print(f"  · {name}: {flag}")
