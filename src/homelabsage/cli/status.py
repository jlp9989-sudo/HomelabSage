"""`homelabsage status` — one-line health summary.

Designed for a terminal status bar / cron heartbeat / "did anything
change?" glance. Aggregates the same data `/api/stack-health` returns
but prints a single line per section in colour. Exits 0 unconditionally
(non-zero exit would conflate "tool failed" with "homelab unhealthy" —
use `homelabsage doctor` for the exit-code form).
"""

from __future__ import annotations

from pathlib import Path

from .. import __version__
from ..audit import build_report
from ..config import load_config
from ..db import Database
from ..models import UpdateStatus
from ..parity import is_parity_running
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging


@app.command(name="status")
def status_cmd(
    config: Path = CONFIG_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """One-line-per-section health summary."""
    setup_logging(verbose)
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)

    # Update counts by status
    counts = {s.value: 0 for s in UpdateStatus}
    for it in db.list(limit=500):
        counts[it.status.value] = counts.get(it.status.value, 0) + 1

    # Audit roll-up
    audit_report = build_report(cfg, db)
    sev = audit_report.counts_by_severity or {}

    # Parity gate state
    parity_active = False
    parity_reason = ""
    if cfg.parity_gate.enabled:
        st = is_parity_running(mdstat_path=cfg.parity_gate.mdstat_path)
        parity_active, parity_reason = st.running, st.reason

    # Pending dispatch queue
    pending = len(db.list_pending_dispatches())

    snoozed = len(db.list_snoozed(limit=500))

    console.print(
        f"[bold]HomelabSage v{__version__}[/bold]"
    )
    console.print(
        f"[cyan]Updates[/cyan]: "
        f"new={counts.get('new', 0)} "
        f"analyzed={counts.get('analyzed', 0)} "
        f"applied={counts.get('applied', 0)} "
        f"dismissed={counts.get('dismissed', 0)} "
        f"failed={counts.get('failed', 0)}"
    )
    console.print(
        f"[cyan]Audit[/cyan]: "
        f"[red]{sev.get('critical', 0)} critical[/red] · "
        f"[red]{sev.get('high', 0)} high[/red] · "
        f"[yellow]{sev.get('medium', 0)} medium[/yellow] · "
        f"[dim]{sev.get('info', 0)} info[/dim]"
    )
    parity_str = (
        f"[yellow]ACTIVE — {parity_reason}[/yellow]"
        if parity_active else "[green]idle[/green]"
    )
    console.print(f"[cyan]Parity gate[/cyan]: {parity_str}")
    pending_colour = "yellow" if pending else "green"
    console.print(
        f"[cyan]Pending dispatches[/cyan]: "
        f"[{pending_colour}]{pending}[/{pending_colour}]"
    )
    snooze_colour = "yellow" if snoozed else "dim"
    console.print(
        f"[cyan]Snoozed updates[/cyan]: "
        f"[{snooze_colour}]{snoozed}[/{snooze_colour}]"
    )
