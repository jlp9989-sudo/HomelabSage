"""`homelabsage audit-mute` — manage the audit-finding mute list.

Operations (sub-verbs):
  - `add CATEGORY SOURCE_KIND SOURCE_REF [--for 7d] [--reason "..."]`
  - `add-from-stdin [--for 7d] [--reason "..."]` — pipes `audit --jsonl`
  - `list [--include-expired]`
  - `remove CATEGORY SOURCE_KIND SOURCE_REF`
  - `purge-expired`

The fingerprint triple is the same one `audit --jsonl` emits per
finding (the `category`, `source_kind`, `source_ref` fields), so the
typical workflow is `audit --jsonl | jq` to pick a row, then
`audit-mute add` with those three values. For bulk muting, pipe the
filtered JSONL directly into `audit-mute add-from-stdin`.
"""

from __future__ import annotations

import json
import re
import sys
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


@audit_mute_app.command(name="purge-expired")
def purge_expired_cmd(
    config: Path = CONFIG_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Drop rows whose `expires_at` is in the past."""
    setup_logging(verbose)
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    n = db.purge_expired_audit_mutes()
    if n == 0:
        console.print("[dim]Nothing to purge.[/dim]")
    else:
        console.print(f"[green]Purged {n} expired mute(s).[/green]")


@audit_mute_app.command(name="add-from-stdin")
def add_from_stdin_cmd(
    config: Path = CONFIG_OPT,
    for_: str = typer.Option("", "--for", help="Relative duration: 7d/12h/30m."),
    until: str = typer.Option("", "--until", help="ISO 8601 UTC expiration."),
    reason: str = typer.Option("", "--reason", help="Free-text reason."),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Bulk-mute fingerprints from JSONL on stdin.

    Each line must be a JSON object with `category`, `source_kind`,
    `source_ref` keys. Lines missing any field are skipped (counted
    in `skipped`). Designed to pair with `audit --jsonl`:

        homelabsage audit --jsonl --severity info \\
          | jq -c 'select(.category=="compose_override")' \\
          | homelabsage audit-mute add-from-stdin --for 30d
    """
    setup_logging(verbose)
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    expires = _resolve_expires(for_ or None, until or None)

    added = 0
    skipped = 0
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(obj, dict):
            skipped += 1
            continue
        cat = obj.get("category")
        src_kind = obj.get("source_kind")
        src_ref = obj.get("source_ref")
        if not (
            isinstance(cat, str) and cat
            and isinstance(src_kind, str) and src_kind
            and isinstance(src_ref, str) and src_ref
        ):
            skipped += 1
            continue
        db.add_audit_mute(
            category=cat, source_kind=src_kind, source_ref=src_ref,
            expires_at=expires, reason=reason or None,
        )
        added += 1
    until_str = expires or "(permanent)"
    console.print(
        f"[green]Muted {added} fingerprint(s)[/green] until {until_str}. "
        f"[dim]Skipped {skipped} malformed line(s).[/dim]"
    )


# Register the sub-Typer on the main `app` as a command group
app.add_typer(audit_mute_app, name="audit-mute")
