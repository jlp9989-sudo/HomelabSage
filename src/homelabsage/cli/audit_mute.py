"""`homelabsage audit-mute` — manage the audit-finding mute list.

Operations (sub-verbs):
  - `add CATEGORY SOURCE_KIND SOURCE_REF [--for 7d] [--reason "..."]`
  - `list [--include-expired]`
  - `remove CATEGORY SOURCE_KIND SOURCE_REF`

The fingerprint triple is the same one `audit --jsonl` emits per
finding (the `category`, `source_kind`, `source_ref` fields), so the
typical workflow is `audit --jsonl | jq` to pick a row, then
`audit-mute add` with those three values.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer

from ..config import load_config
from ..db import Database
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging

audit_mute_app = typer.Typer(
    help="Manage the audit-finding mute list.",
    no_args_is_help=True,
)

_REL_RE = re.compile(r"^\s*(\d+)\s*([dhm])\s*$", re.IGNORECASE)


def _resolve_expires(for_: str | None, until: str | None) -> str | None:
    if for_ and until:
        raise typer.BadParameter("pass either --for OR --until, not both")
    if until:
        # Validate that it parses as ISO.
        try:
            datetime.fromisoformat(until)
        except ValueError as e:
            raise typer.BadParameter(
                f"--until not ISO 8601: {until!r}"
            ) from e
        return until
    if for_:
        m = _REL_RE.match(for_)
        if m is None:
            raise typer.BadParameter("--for must be `7d` / `12h` / `30m`")
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {
            "d": timedelta(days=n),
            "h": timedelta(hours=n),
            "m": timedelta(minutes=n),
        }[unit]
        return (datetime.now(UTC) + delta).isoformat()
    return None


@audit_mute_app.command(name="add")
def add_cmd(
    category: str = typer.Argument(...),
    source_kind: str = typer.Argument(...),
    source_ref: str = typer.Argument(...),
    config: Path = CONFIG_OPT,
    for_: str = typer.Option("", "--for", help="Relative duration: 7d/12h/30m."),
    until: str = typer.Option("", "--until", help="ISO 8601 UTC expiration."),
    reason: str = typer.Option("", "--reason", help="Free-text reason."),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Mute one finding fingerprint."""
    setup_logging(verbose)
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    expires = _resolve_expires(for_ or None, until or None)
    db.add_audit_mute(
        category=category, source_kind=source_kind,
        source_ref=source_ref, expires_at=expires,
        reason=reason or None,
    )
    until_str = expires or "(permanent)"
    console.print(
        f"[green]Muted[/green] {category}/{source_kind}/{source_ref} "
        f"until {until_str}"
    )


@audit_mute_app.command(name="list")
def list_cmd(
    config: Path = CONFIG_OPT,
    include_expired: bool = typer.Option(
        False, "--include-expired",
        help="Show mutes whose expires_at is in the past.",
    ),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """List active mutes (or include expired)."""
    setup_logging(verbose)
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    rows = db.list_audit_mutes(include_expired=include_expired)
    if not rows:
        console.print("[dim]No mutes.[/dim]")
        return
    for r in rows:
        exp = r["expires_at"] or "permanent"
        reason = r["reason"] or ""
        console.print(
            f"[cyan]{r['category']}[/cyan]/[cyan]{r['source_kind']}[/cyan]/"
            f"{r['source_ref']} — until [yellow]{exp}[/yellow] "
            f"[dim]{reason}[/dim]"
        )


@audit_mute_app.command(name="remove")
def remove_cmd(
    category: str = typer.Argument(...),
    source_kind: str = typer.Argument(...),
    source_ref: str = typer.Argument(...),
    config: Path = CONFIG_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Drop one mute by fingerprint."""
    setup_logging(verbose)
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    ok = db.remove_audit_mute(
        category=category, source_kind=source_kind, source_ref=source_ref,
    )
    if ok:
        console.print(
            f"[green]Removed[/green] mute on "
            f"{category}/{source_kind}/{source_ref}"
        )
    else:
        console.print(
            f"[yellow]No mute found for[/yellow] "
            f"{category}/{source_kind}/{source_ref}"
        )


# Register the sub-Typer on the main `app` as a command group
app.add_typer(audit_mute_app, name="audit-mute")
