"""`homelabsage curate` — generate per-container Markdown notes or the system note."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from ..config import load_config
from ..db import Database
from ._common import (
    CONFIG_OPT,
    CURATE_TARGET_OPT,
    VERBOSE_OPT,
    app,
    console,
    setup_logging,
)


@app.command()
def curate(
    config: Path = CONFIG_OPT,
    discover: bool = typer.Option(
        False, "--discover",
        help="Process every running container with a resolvable repo.",
    ),
    target: list[str] | None = CURATE_TARGET_OPT,
    limit: int = typer.Option(0, "--limit", help="Cap on containers processed. 0 = no cap."),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Print the would-be note instead of writing it.",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Regenerate even if the note exists with matching digest or no curator footer.",
    ),
    show_prompt: bool = typer.Option(
        False, "--show-prompt",
        help="Print the rendered prompt for each target and exit.",
    ),
    system: bool = typer.Option(
        False, "--system",
        help=(
            "Generate `notes/system.md` from host probes (kernel, docker info, "
            "GPU, ZFS, Unraid). Independent of --discover/--target; can run "
            "alone or alongside them in the same invocation."
        ),
    ),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Generate one Markdown note per running container.

    Use `--discover` to walk every running container, or `--target NAME` (one
    or more times) to curate specific ones. `--dry-run` prints the proposed
    note without touching the filesystem.
    """
    setup_logging(verbose)
    if not discover and not (target or []) and not system:
        console.print(
            "[red]One of --discover, --target, --system is required.[/red]"
        )
        raise typer.Exit(code=2)

    cfg = load_config(config)
    if not cfg.curator.enabled:
        console.print("[yellow]curator.enabled is false in config — aborting.[/yellow]")
        raise typer.Exit(code=1)

    if system:
        # Lazy import — pulls in subprocess + filesystem probes only when needed.
        from ..curator.system import curate_system

        notes_dir = cfg.curator.output_dir or cfg.notes.notes_dir
        if not notes_dir:
            console.print(
                "[yellow]No notes_dir / curator.output_dir configured — "
                "system.md would have nowhere to live.[/yellow]"
            )
            raise typer.Exit(code=1)
        path, report = curate_system(notes_dir)
        if path is None:
            console.print("[red]system.md write failed (check logs).[/red]")
            raise typer.Exit(code=1)
        console.print(
            f"[green]system.md[/green] written to {path} "
            f"[dim](fingerprint {report.fingerprint()})[/dim]"
        )
        # When --system is the only flag, exit cleanly without entering the
        # per-container path below.
        if not discover and not (target or []):
            return

    # Lazy import so the heavy docker SDK only loads for this subcommand.
    from ..curator import Curator
    from ..llm import LLMClient

    llm = LLMClient(cfg.llm)
    db = Database(cfg.storage.database_path)
    curator = Curator(
        cfg.curator,
        llm,
        cfg.sources.docker,
        notes_dir=cfg.notes.notes_dir,
        db=db,
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
                "interview_pending": "magenta",
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
            if result.status == "interview_pending" and result.interview_question_id:
                console.print(
                    f"  [magenta]→ run `homelabsage interview answer "
                    f"{result.interview_question_id} --text \"...\"`[/magenta]"
                )
    finally:
        curator.close()
        db.close()
