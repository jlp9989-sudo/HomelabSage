"""HomelabSage — Typer-based CLI.

Commands:
  homelabsage check                Run a single scan cycle and exit.
  homelabsage list  [--source X]   List stored updates.
  homelabsage curate               Generate per-container Markdown notes.
  homelabsage export               Sanitised dump for issues / support.
  homelabsage serve                Start the web UI + scheduler.
  homelabsage version              Print version.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import load_config
from .db import Database
from .engine import run_blocking
from .models import UpdateStatus

app = typer.Typer(add_completion=False, help="HomelabSage — AI-powered homelab analyzer.")
console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


CONFIG_OPT = typer.Option(
    "config.yaml", "--config", "-c", help="Path to config.yaml.", show_default=True
)
VERBOSE_OPT = typer.Option(False, "--verbose", "-v", help="Debug logging.")
CURATE_TARGET_OPT = typer.Option(
    None, "--target", "-t", help="Container name to curate. Repeat for several."
)
EXPORT_OUTPUT_OPT = typer.Option(
    Path("-"), "--output", "-o", help="Output path. `-` writes to stdout."
)
EXPORT_REDACT_OPT = typer.Option(
    True,
    "--redact/--no-redact",
    help="Strip IPs, hostnames, and credentials. Default: on. "
         "Turn off only when piping into a tool you fully trust.",
)


@app.command()
def check(config: Path = CONFIG_OPT, verbose: bool = VERBOSE_OPT) -> None:
    """Run a single scan + analyze + output cycle."""
    _setup_logging(verbose)
    cfg = load_config(config)
    stats = run_blocking(cfg)
    console.print(f"[green]Done.[/green] {stats}")


@app.command("list")
def list_cmd(
    config: Path = CONFIG_OPT,
    source: str | None = typer.Option(None, help="Filter by plugin id (e.g. docker)."),
    status: str | None = typer.Option(None, help="Filter by status."),
    limit: int = typer.Option(50, help="Max rows."),
) -> None:
    """Show stored updates."""
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    status_enum = UpdateStatus(status) if status else None
    items = db.list(status=status_enum, source=source, limit=limit)
    db.close()
    if not items:
        console.print("[dim]No updates stored yet.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Source")
    table.add_column("Subject")
    table.add_column("Current → New")
    table.add_column("Severity")
    table.add_column("Status")
    table.add_column("Summary", overflow="fold", max_width=60)
    for it in items:
        u = it.update
        a = it.analysis
        table.add_row(
            u.source,
            u.subject,
            f"{u.current_version} → {u.new_version}",
            a.severity.value if a else "-",
            it.status.value,
            (a.summary if a else "")[:140],
        )
    console.print(table)


@app.command()
def curate(
    config: Path = CONFIG_OPT,
    discover: bool = typer.Option(
        False, "--discover", help="Process every running container with a resolvable repo."
    ),
    target: list[str] | None = CURATE_TARGET_OPT,
    limit: int = typer.Option(0, "--limit", help="Cap on containers processed. 0 = no cap."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the would-be note instead of writing it."
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Regenerate even if the note exists with matching digest or no curator footer.",
    ),
    show_prompt: bool = typer.Option(
        False, "--show-prompt", help="Print the rendered prompt for each target and exit."
    ),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Generate one Markdown note per running container.

    Use `--discover` to walk every running container, or `--target NAME` (one
    or more times) to curate specific ones. `--dry-run` prints the proposed
    note without touching the filesystem.
    """
    _setup_logging(verbose)
    if not discover and not (target or []):
        console.print(
            "[red]Either --discover or at least one --target is required.[/red]"
        )
        raise typer.Exit(code=2)

    cfg = load_config(config)
    if not cfg.curator.enabled:
        console.print("[yellow]curator.enabled is false in config — aborting.[/yellow]")
        raise typer.Exit(code=1)

    # Lazy import so the heavy docker SDK only loads for this subcommand.
    from .curator import Curator
    from .llm import LLMClient

    llm = LLMClient(cfg.llm)
    curator = Curator(
        cfg.curator,
        llm,
        cfg.sources.docker,
        notes_dir=cfg.notes.notes_dir,
    )
    try:
        snapshots = curator.discover_targets(
            limit=limit or None,
            only=list(target) if target else None,  # target may be None
        )
        if not snapshots:
            console.print("[dim]No matching containers found.[/dim]")
            return

        if show_prompt:
            for snap in snapshots:
                release_notes = asyncio.run(curator.fetch_release_context(snap.repo))
                examples = curator.load_style_examples()
                rendered = curator.build_prompt(snap, release_notes, examples)
                console.rule(f"[bold]{snap.name}[/bold]")
                console.print(rendered)
            return

        for snap in snapshots:
            result = asyncio.run(curator.curate_one(snap, dry_run=dry_run, force=force))
            label_color = {
                "written": "green",
                "skipped_same_digest": "dim",
                "skipped_manual": "yellow",
                "skipped_dry_run": "cyan",
                "llm_failed": "red",
            }.get(result.status, "white")
            path_str = str(result.path) if result.path else "(no path)"
            console.print(
                f"[{label_color}]{result.status:<22}[/{label_color}] "
                f"{snap.name:<28} {path_str}"
            )
            if dry_run and result.body:
                console.rule(f"[bold]{snap.name}[/bold]")
                console.print(result.body)
            if result.note:
                console.print(f"  [dim]{result.note}[/dim]")
    finally:
        curator.close()


def _collect_export_payload(cfg) -> dict:
    """Build the unsanitised export payload — kept thin so the redaction layer
    in `homelabsage.redact` is the only thing we have to unit-test for safety.

    Lives in `cli.py` rather than a sibling module because it's the only caller
    and the docker SDK is already imported by the docker plugin lazily.
    """
    from . import __version__ as version_str

    payload: dict = {
        "homelabsage_version": version_str,
        "containers": [],
        "recent_updates": [],
    }

    # Containers — read via the same path the docker plugin uses, but we
    # don't compare versions here, just collect.
    if cfg.sources.docker.enabled:
        try:
            import docker as docker_sdk

            client = docker_sdk.DockerClient(
                base_url=f"unix://{cfg.sources.docker.socket.lstrip('/')}"
            )
            for c in client.containers.list(all=True):
                env_list = (c.attrs.get("Config") or {}).get("Env") or []
                env_dict: dict[str, str] = {}
                for line in env_list:
                    if "=" in line:
                        k, v = line.split("=", 1)
                        env_dict[k] = v
                payload["containers"].append({
                    "name": c.name,
                    "image": c.image.tags[0] if c.image.tags else "",
                    "status": (c.attrs.get("State") or {}).get("Status", ""),
                    "ports": list(
                        ((c.attrs.get("NetworkSettings") or {}).get("Ports") or {}).keys()
                    ),
                    "mounts": [
                        {"src": m.get("Source"), "dst": m.get("Destination"), "rw": m.get("RW")}
                        for m in (c.attrs.get("Mounts") or [])
                    ],
                    "env": env_dict,
                    "labels": (c.attrs.get("Config") or {}).get("Labels") or {},
                    "restart_policy": ((c.attrs.get("HostConfig") or {})
                                       .get("RestartPolicy") or {}).get("Name", ""),
                })
        except Exception as e:
            log = logging.getLogger(__name__)
            log.warning("Docker inventory failed in export: %s", e)

    # Recent analyses
    db = Database(cfg.storage.database_path)
    for it in db.list(limit=50):
        payload["recent_updates"].append({
            "source": it.update.source,
            "subject": it.update.subject,
            "current_version": it.update.current_version,
            "new_version": it.update.new_version,
            "release_url": it.update.release_url,
            "severity": it.analysis.severity.value if it.analysis else None,
            "summary": it.analysis.summary if it.analysis else None,
            "status": it.status.value,
            "context": it.update.context,
        })
    db.close()

    return payload


@app.command()
def export(
    config: Path = CONFIG_OPT,
    output: Path = EXPORT_OUTPUT_OPT,
    redact: bool = EXPORT_REDACT_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Dump containers + recent analyses as one JSON file, safe to share.

    With `--redact` (default) every value goes through the sanitiser in
    `homelabsage.redact`: IPs become `10.0.0.N`, hostnames `host-N`, and
    keys matching `*_PASSWORD|*_TOKEN|*_KEY|*_SECRET|*_AUTH` or values
    matching common credential shapes (JWT, GitHub PAT, OpenAI sk-…) become
    `<redacted>`. Allowlist preserves loopback IPs and public services like
    `github.com` / `ghcr.io` / `api.openai.com`.
    """
    _setup_logging(verbose)
    cfg = load_config(config)
    payload = _collect_export_payload(cfg)
    if redact:
        from .redact import Sanitiser

        payload = Sanitiser().sanitise(payload)

    text = json.dumps(payload, indent=2, default=str)
    if str(output) == "-":
        sys.stdout.write(text + "\n")
    else:
        output.write_text(text + "\n")
        console.print(f"[green]Wrote[/green] {len(text)} bytes to {output}")


@app.command()
def serve(config: Path = CONFIG_OPT, verbose: bool = VERBOSE_OPT) -> None:
    """Run the web UI + background scheduler (long-running)."""
    _setup_logging(verbose)
    cfg = load_config(config)
    # Lazy import to keep CLI startup fast.
    from .web import run_web

    run_web(cfg)


@app.command()
def version() -> None:
    console.print(__version__)


if __name__ == "__main__":
    app()
