"""`homelabsage scripts` — enumerate cron / systemd / Unraid user scripts."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from ..config import load_config
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging


@app.command()
def scripts(
    config: Path = CONFIG_OPT,
    enable_unraid: bool = typer.Option(
        False, "--unraid",
        help="Also probe Unraid User Scripts (`/boot/config/...`).",
    ),
    no_systemd: bool = typer.Option(
        False, "--no-systemd",
        help="Skip the `systemctl list-timers` probe.",
    ),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Enumerate scheduled jobs and scripts the host will run.

    Prints one row per probe hit: cron entries from `/etc/crontab`,
    `/etc/cron.d`, periodic dirs, user crontabs in `/var/spool/cron`,
    plus `systemctl list-timers` and (opt-in) Unraid User Scripts.

    Read-only: this command doesn't modify anything. It's the diagnostic
    counterpart of `docker ps` for the time-based half of a homelab.
    """
    setup_logging(verbose)
    cfg = load_config(config)
    # Module name collision with this file — import directly from package.
    from ..scripts import DiscoveryRoots, discover_scripts

    sc = cfg.sources.scripts
    roots = DiscoveryRoots(
        etc=Path(sc.etc_root),
        cron_spool=Path(sc.cron_spool),
        unraid_user_scripts=Path(sc.unraid_scripts_root),
        systemctl=sc.systemctl_path,
    )
    found = discover_scripts(
        roots,
        enable_systemd=(not no_systemd) and sc.enable_systemd,
        enable_unraid=enable_unraid or sc.enable_unraid,
    )
    if not found:
        console.print(
            "[dim]No scheduled jobs found. "
            "Use --unraid if you're on Unraid, or --verbose for debug logs.[/dim]"
        )
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Source")
    table.add_column("Schedule")
    table.add_column("Name")
    table.add_column("Command", overflow="fold", max_width=60)
    for s in found:
        table.add_row(
            s.source, s.schedule, s.name,
            (s.command or "(script body, see " + s.path + ")")[:140],
        )
    console.print(table)
    console.print(
        f"\n[dim]{len(found)} entries from "
        f"{len({s.source for s in found})} sources.[/dim]"
    )
