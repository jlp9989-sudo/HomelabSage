"""`homelabsage snooze` — silence an update's push notifications.

Operations:
  - `homelabsage snooze <id> --until 2026-06-10T00:00:00Z` set snooze
  - `homelabsage snooze <id> --for 7d` relative shorthand (d/h/m)
  - `homelabsage snooze <id> --clear` clear snooze
  - `homelabsage snooze --list` list currently-snoozed updates

Snooze SUPPRESSES push outputs only. Notion writeback + dashboard
visibility are unchanged.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer

from .._time import parse_iso
from ..config import load_config
from ..db import Database
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging

_REL_RE = re.compile(r"^\s*(\d+)\s*([dhm])\s*$", re.IGNORECASE)


def _resolve_until(
    until: str | None,
    for_: str | None,
) -> str | None:
    """Return ISO UTC string from --until / --for, or None if neither."""
    if until and for_:
        raise typer.BadParameter("pass either --until OR --for, not both")
    if until:
        dt = parse_iso(until)
        if dt is None:
            raise typer.BadParameter(f"--until not ISO 8601: {until!r}")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.isoformat()
    if for_:
        m = _REL_RE.match(for_)
        if m is None:
            raise typer.BadParameter(
                "--for must look like `7d` / `12h` / `30m`"
            )
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {
            "d": timedelta(days=n),
            "h": timedelta(hours=n),
            "m": timedelta(minutes=n),
        }[unit]
        return (datetime.now(UTC) + delta).isoformat()
    return None


@app.command(name="snooze")
def snooze_cmd(
    update_id: str = typer.Argument(
        "", help="Update id to snooze. Omit when using --list.",
    ),
    config: Path = CONFIG_OPT,
    until: str = typer.Option(
        "", "--until",
        help="ISO 8601 UTC timestamp to snooze until (e.g. 2026-06-10T00:00:00Z).",
    ),
    for_: str = typer.Option(
        "", "--for",
        help="Relative duration: `7d` / `12h` / `30m`.",
    ),
    clear: bool = typer.Option(
        False, "--clear",
        help="Clear the snooze on this update.",
    ),
    list_: bool = typer.Option(
        False, "--list",
        help="List currently-snoozed updates.",
    ),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Set / clear / list snoozes on updates."""
    setup_logging(verbose)
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)

    if list_:
        rows = db.list_snoozed(limit=500)
        if not rows:
            console.print("[dim]No active snoozes.[/dim]")
            raise SystemExit(0)
        for r in rows:
            console.print(
                f"[yellow]{r['snooze_until']}[/yellow] "
                f"[bold]{r['subject']}[/bold] "
                f"[dim]({r['source']}, id={r['id'][:12]}…)[/dim]"
            )
        raise SystemExit(0)

    if not update_id:
        console.print(
            "[red]Pass an update id, or use --list to see existing snoozes.[/red]"
        )
        raise SystemExit(2)

    if clear:
        ok = db.set_snooze(update_id, None)
        if not ok:
            console.print(f"[red]No update with id={update_id}[/red]")
            raise SystemExit(2)
        console.print(f"[green]Cleared snooze on {update_id}[/green]")
        raise SystemExit(0)

    resolved = _resolve_until(until or None, for_ or None)
    if resolved is None:
        console.print(
            "[red]Pass --until, --for, or --clear.[/red]"
        )
        raise SystemExit(2)
    ok = db.set_snooze(update_id, resolved)
    if not ok:
        console.print(f"[red]No update with id={update_id}[/red]")
        raise SystemExit(2)
    console.print(
        f"[green]Snoozed {update_id} until {resolved}[/green]"
    )
    raise SystemExit(0)
