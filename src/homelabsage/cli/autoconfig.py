"""`homelabsage autoconfig` — detect config from the host, review, apply.

Read-only by default: prints every proposal with its evidence so the user
reviews before anything is written. `--apply` writes all of them to the
user overlay (`config.user.yaml`) through the same validated path the
settings UI uses; `--json` emits machine-readable proposals for scripts.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

import typer

from ..autoconfig import apply_proposals, build_proposals
from ..config import load_config
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging


@app.command(name="autoconfig")
def autoconfig(
    config: Path = CONFIG_OPT,
    apply: bool = typer.Option(
        False, "--apply",
        help="Write every proposal to the user overlay (config.user.yaml).",
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit proposals as JSON instead of a table.",
    ),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Detect config values from this host and propose (or apply) them."""
    setup_logging(verbose)
    cfg = load_config(config)
    proposals = build_proposals(cfg)

    if json_out:
        console.print(_json.dumps(
            [p.to_dict() for p in proposals], indent=2, default=str,
        ))
        return

    if not proposals:
        console.print(
            "[green]Nothing to propose — the config already reflects "
            "what this host exposes.[/green]"
        )
        return

    for i, p in enumerate(proposals, 1):
        console.print(f"[bold]{i}. {p.dotted}[/bold]")
        console.print(f"   current:  {p.current!r}")
        console.print(f"   proposed: {p.value!r}")
        console.print(f"   [dim]why: {p.evidence}[/dim]")

    if not apply:
        console.print(
            f"\n[yellow]{len(proposals)} proposal(s) — review above, then "
            f"re-run with --apply (or pick individually in the web UI at "
            f"/autoconfig).[/yellow]"
        )
        return

    applied = apply_proposals(config, proposals)
    console.print(
        f"\n[green]Applied {len(applied)} proposal(s) to the user overlay. "
        f"Restart the service so the engine picks them up.[/green]"
    )
