"""`homelabsage interview` sub-app — manage curator rule-7 interview questions."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.table import Table

from ..config import load_config
from ..db import Database
from ..models import InterviewStatus
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging

interview_app = typer.Typer(
    add_completion=False,
    help="Manage curator interview questions (Rule 7 fallbacks).",
    no_args_is_help=True,
)
app.add_typer(interview_app, name="interview")


@interview_app.command("list")
def interview_list(
    config: Path = CONFIG_OPT,
    status: str = typer.Option(
        "pending", "--status", "-s",
        help="Filter by status: pending | answered | dismissed | all.",
    ),
    limit: int = typer.Option(50, help="Max rows."),
) -> None:
    """List curator interview questions."""
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    try:
        if status == "all":
            questions = db.list_interview_questions(status=None, limit=limit)
        else:
            questions = db.list_interview_questions(
                status=InterviewStatus(status), limit=limit
            )
        if not questions:
            console.print(f"[dim]No interview questions with status={status}.[/dim]")
            return
        table = Table(show_header=True, header_style="bold")
        table.add_column("ID", justify="right")
        table.add_column("Container")
        table.add_column("Status")
        table.add_column("Suggestion", overflow="fold", max_width=40)
        table.add_column("Answer", overflow="fold", max_width=40)
        for q in questions:
            table.add_row(
                str(q.id),
                q.container_name,
                q.status.value,
                q.suggested_text or "[dim](none)[/dim]",
                (q.answer_text or "")[:140],
            )
        console.print(table)
    finally:
        db.close()


@interview_app.command("answer")
def interview_answer(
    question_id: int = typer.Argument(..., help="ID of the question to answer."),
    text: str = typer.Option(..., "--text", "-t", help="Your answer text."),
    config: Path = CONFIG_OPT,
    no_recurate: bool = typer.Option(
        False, "--no-recurate",
        help="Just record the answer, don't regenerate the note.",
    ),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Answer an interview question and re-curate the container's note."""
    setup_logging(verbose)
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    try:
        q = db.get_interview_question(question_id)
        if q is None:
            console.print(f"[red]No question with id={question_id}.[/red]")
            raise typer.Exit(code=1)
        db.answer_interview_question(question_id, text)
        console.print(
            f"[green]Recorded answer[/green] for question #{question_id} "
            f"(container={q.container_name})."
        )
        if no_recurate:
            return

        # Re-curate that one container with user_purpose injected.
        from ..curator import Curator
        from ..llm import LLMClient

        llm = LLMClient(cfg.llm)
        curator = Curator(
            cfg.curator,
            llm,
            cfg.sources.docker,
            notes_dir=cfg.notes.notes_dir,
            db=db,
        )
        try:
            snapshots = curator.discover_targets(only=[q.container_name])
            if not snapshots:
                console.print(
                    f"[yellow]Container {q.container_name} not running — "
                    f"answer recorded but note not regenerated.[/yellow]"
                )
                return
            result = asyncio.run(
                curator.curate_one(snapshots[0], force=True, user_purpose=text)
            )
            console.print(
                f"[green]Re-curated[/green] {q.container_name} → status={result.status}"
            )
            if result.path:
                console.print(f"  {result.path}")
        finally:
            curator.close()
    finally:
        db.close()


@interview_app.command("cleanup")
def interview_cleanup(
    config: Path = CONFIG_OPT,
    days: int = typer.Option(
        30, "--days",
        help="Auto-dismiss PENDING questions older than this many days.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print what WOULD be dismissed; touch nothing.",
    ),
) -> None:
    """Cron-friendly auto-dismiss of pending interview questions that have
    aged out. Useful when the curator emitted a question the user never
    intends to answer (one-off containers, throwaway experiments)."""
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    try:
        if dry_run:
            from datetime import timedelta

            from .._time import utcnow
            cutoff = utcnow() - timedelta(days=days)
            rows = db.list_interview_questions(
                status=InterviewStatus.PENDING, limit=10000,
            )
            stale = [
                q for q in rows
                if q.created_at and q.created_at < cutoff
            ]
            console.print(
                f"[yellow]Would dismiss {len(stale)} questions[/yellow] older than {days} days."
            )
            for q in stale[:10]:
                created = q.created_at.strftime("%Y-%m-%d") if q.created_at else "?"
                console.print(f"  #{q.id} {q.container_name} (created {created})")
            if len(stale) > 10:
                console.print(f"  … and {len(stale) - 10} more")
            return
        n = db.dismiss_stale_interview_questions(older_than_days=days)
        console.print(
            f"[green]Auto-dismissed {n} stale question(s)[/green] older than {days} days."
        )
    finally:
        db.close()


@interview_app.command("dismiss")
def interview_dismiss(
    question_id: int = typer.Argument(..., help="ID of the question to dismiss."),
    config: Path = CONFIG_OPT,
) -> None:
    """Dismiss a question — the curator will not ask again until the image rebuilds."""
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    try:
        q = db.get_interview_question(question_id)
        if q is None:
            console.print(f"[red]No question with id={question_id}.[/red]")
            raise typer.Exit(code=1)
        db.dismiss_interview_question(question_id)
        console.print(
            f"[green]Dismissed[/green] question #{question_id} (container={q.container_name})."
        )
    finally:
        db.close()
