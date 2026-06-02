"""`homelabsage watchtower-migrate` — produce a migration report.

Read-only inventory: detects a running Watchtower container, parses its
env/args, lists every container it was managing, and prints recommended
HomelabSage routing per container. Useful right after the Dec-2025
upstream archival when users come looking for a replacement.
"""

from __future__ import annotations

from pathlib import Path

from ..config import load_config
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging


@app.command("watchtower-migrate")
def watchtower_migrate(
    config: Path = CONFIG_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Detect Watchtower in your Docker stack and print a migration plan."""
    setup_logging(verbose)
    cfg = load_config(config)
    if not cfg.sources.docker.enabled:
        console.print(
            "[yellow]sources.docker is disabled in config — nothing to scan.[/yellow]"
        )
        return

    # Lazy import: docker SDK is heavy and only needed here.
    import docker as docker_sdk

    from ..watchtower_migrate import detect

    try:
        client = docker_sdk.DockerClient(
            base_url=f"unix://{cfg.sources.docker.socket.lstrip('/')}"
        )
        containers = client.containers.list(all=True)
    except Exception as e:
        console.print(f"[red]Docker daemon unreachable: {e}[/red]")
        return

    report = detect(containers)
    if report is None:
        console.print(
            "[dim]No Watchtower container detected. "
            "If you're not running Watchtower, nothing to migrate.[/dim]"
        )
        return

    console.print(report.to_markdown())
