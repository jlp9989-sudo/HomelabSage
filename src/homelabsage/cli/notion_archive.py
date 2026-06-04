"""`homelabsage notion-archive` — set `archived=true` on Notion pages
for updates older than N days.

The Notion DB grows linearly with scan history. After a year or two of
HomelabSage running, the dashboard becomes a wall of old "applied"
rows. This CLI walks every update older than `--days` whose
`notion_page_id` is set, and PATCHes `archived: true` on each page.

Pure best-effort: missing notion_page_id, 404s, missing token all
silently skip + log. Exits with row counts so the user knows what
happened.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import httpx
import typer

from .._time import utcnow
from ..config import load_config
from ..db import Database
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging

_NOTION_API = "https://api.notion.com/v1"


async def _archive_one(
    page_id: str, *, token: str, client: httpx.AsyncClient,
) -> bool:
    url = f"{_NOTION_API}/pages/{page_id}"
    try:
        r = await client.patch(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json",
            },
            json={"archived": True},
        )
        if r.status_code in (200, 201):
            return True
        if r.status_code == 404:
            return False  # page already gone
        return False
    except httpx.HTTPError:
        return False


@app.command(name="notion-archive")
def notion_archive(
    config: Path = CONFIG_OPT,
    days: int = typer.Option(
        90, "--days",
        help="Archive Notion pages whose update is older than N days.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Print the count that WOULD be archived; touch nothing.",
    ),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Archive stale Notion pages so the database stays readable."""
    setup_logging(verbose)
    cfg = load_config(config)
    if not cfg.outputs.notion.enabled or not cfg.outputs.notion.api_key:
        console.print("[yellow]Notion output is not configured.[/yellow]")
        raise SystemExit(0)
    cutoff = utcnow() - timedelta(days=days)
    db = Database(cfg.storage.database_path)
    try:
        # Walk every row with a notion_page_id detected before the cutoff.
        rows = [
            it for it in db.list(limit=10000)
            if it.notion_page_id and it.detected_at and it.detected_at < cutoff
        ]
    finally:
        db.close()
    if dry_run:
        console.print(
            f"[yellow]Would archive {len(rows)} pages[/yellow] older than {days} days."
        )
        for it in rows[:10]:
            console.print(f"  {it.id} (page {it.notion_page_id})")
        if len(rows) > 10:
            console.print(f"  ... and {len(rows) - 10} more")
        raise SystemExit(0)

    if not rows:
        console.print("[green]Nothing to archive.[/green]")
        raise SystemExit(0)

    async def _run():
        archived = 0
        async with httpx.AsyncClient(timeout=15) as client:
            for it in rows:
                if await _archive_one(
                    it.notion_page_id,
                    token=cfg.outputs.notion.api_key,
                    client=client,
                ):
                    archived += 1
        return archived

    n = asyncio.run(_run())
    console.print(
        f"[green]Archived {n}/{len(rows)} Notion pages[/green] older than {days} days."
    )
