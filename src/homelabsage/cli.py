"""HomelabSage — Typer-based CLI.

Commands:
  homelabsage check                Run a single scan cycle and exit.
  homelabsage list  [--source X]   List stored updates.
  homelabsage curate               Generate per-container Markdown notes.
  homelabsage scripts              Enumerate cron / systemd / Unraid scripts.
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
from .models import InterviewStatus, UpdateStatus

log = logging.getLogger(__name__)

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
DIGEST_CHANNEL_OPT = typer.Option(
    None,
    "--channel",
    help=(
        "Restrict delivery to specific channels (telegram|discord|ntfy|"
        "gotify). Repeat for several. Defaults to every enabled channel."
    ),
)


@app.command()
def check(config: Path = CONFIG_OPT, verbose: bool = VERBOSE_OPT) -> None:
    """Run a single scan + analyze + output cycle."""
    _setup_logging(verbose)
    cfg = load_config(config)
    stats = run_blocking(cfg, cfg_path=config)
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
    system: bool = typer.Option(
        False,
        "--system",
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
    _setup_logging(verbose)
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
        from .curator.system import curate_system

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
    from .curator import Curator
    from .llm import LLMClient

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
def scripts(
    config: Path = CONFIG_OPT,
    enable_unraid: bool = typer.Option(
        False, "--unraid", help="Also probe Unraid User Scripts (`/boot/config/...`)."
    ),
    no_systemd: bool = typer.Option(
        False, "--no-systemd", help="Skip the `systemctl list-timers` probe."
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
    _setup_logging(verbose)
    cfg = load_config(config)
    from .scripts import DiscoveryRoots, discover_scripts

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
        console.print("[dim]No scheduled jobs found. "
                      "Use --unraid if you're on Unraid, or --verbose for debug logs.[/dim]")
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
    console.print(f"\n[dim]{len(found)} entries from "
                  f"{len({s.source for s in found})} sources.[/dim]")


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


@app.command("notify-pending")
def notify_pending(
    config: Path = CONFIG_OPT,
    hours: int = typer.Option(
        48,
        "--hours",
        help=(
            "Look back this many hours for analyzed items whose push "
            "notification may have been skipped (parity gate, transient "
            "outage). Default 48h."
        ),
    ),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Replay push notifications for recently analyzed items.

    Useful after a parity check finishes and you want the notifications
    that were skipped to fire now. The push outputs themselves are
    idempotent in spirit but NOT in practice (Telegram has no message
    de-duplication) — re-running this with a small window is safe; running
    with `--hours 720` will spam a month of notifications, so don't.
    """
    _setup_logging(verbose)
    from datetime import datetime, timedelta

    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    try:
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        # build_outputs already filters by enabled flag
        from .engine import build_outputs
        outputs = [o for o in build_outputs(cfg, db) if o.is_push]
        if not outputs:
            console.print("[yellow]No push outputs enabled — nothing to flush.[/yellow]")
            return

        items = [
            it for it in db.list(limit=500)
            if it.analyzed_at and it.analyzed_at >= cutoff and it.analysis
        ]
        if not items:
            console.print(f"[dim]No analyzed items in the last {hours}h.[/dim]")
            return

        async def _flush():
            for it in items:
                for out in outputs:
                    try:
                        await out.send(it)
                    except Exception as e:
                        log.exception("notify-pending: %s on %s: %s", out.id, it.id, e)

        asyncio.run(_flush())
        console.print(
            f"[green]Replayed[/green] {len(items)} item(s) across "
            f"{len(outputs)} push output(s)."
        )
    finally:
        db.close()


@app.command()
def analyse(
    url: str = typer.Argument(..., help="GitHub or Codeberg repo URL."),
    config: Path = CONFIG_OPT,
    version: str = typer.Option(
        "",
        "--version",
        help="Optional current version you run locally. Improves the analysis.",
    ),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """One-shot analysis of a pasted repo URL. Uses your configured LLM + notes."""
    _setup_logging(verbose)
    cfg = load_config(config)
    from .analyse_url import analyse_repo_url, parse_repo_url

    repo = parse_repo_url(url)
    if repo is None:
        console.print(
            f"[red]Could not parse a GitHub/Codeberg repo URL from {url!r}.[/red]"
        )
        raise typer.Exit(code=2)
    analyzed = asyncio.run(analyse_repo_url(cfg, url, current_version=version))
    if analyzed is None:
        console.print(f"[red]Analysis failed for {repo}.[/red]")
        raise typer.Exit(code=1)
    u = analyzed.update
    a = analyzed.analysis
    console.print(f"[bold]{u.subject}[/bold]  {u.current_version} → {u.new_version}")
    if u.release_url:
        console.print(f"[dim]{u.release_url}[/dim]")
    if a is None:
        console.print("[yellow]LLM analysis unavailable — printed Update only.[/yellow]")
        return
    sev_colour = {"critical": "red", "high": "yellow",
                  "medium": "cyan", "info": "blue"}.get(a.severity.value, "white")
    console.print(
        f"\n[{sev_colour}]{a.severity.value.upper()}[/{sev_colour}] — {a.summary}\n"
    )
    if a.breaking_changes:
        console.print("[bold]Breaking changes:[/bold]")
        for bc in a.breaking_changes:
            console.print(f"  • {bc}")
    if a.new_features_relevant:
        console.print("\n[bold]New features:[/bold]")
        for nf in a.new_features_relevant:
            console.print(f"  • {nf}")
    if a.recommended_action:
        console.print(f"\n[bold]Recommended action:[/bold] {a.recommended_action}")


@app.command()
def csi(
    container: str = typer.Argument(..., help="Container name (as in `docker ps`)."),
    config: Path = CONFIG_OPT,
    evidence_only: bool = typer.Option(
        False,
        "--evidence-only",
        help="Skip the LLM call — print only the gathered logs/notes/update.",
    ),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Post-mortem assistant for a single container.

    Pulls the last detected update + container logs since that timestamp,
    filters to ERROR/WARN/FATAL lines, cross-references your notes, and
    asks the LLM to diagnose. Use --evidence-only to skip the LLM and just
    print the raw signals.
    """
    _setup_logging(verbose)
    cfg = load_config(config)
    from .csi import (
        _evidence_only_report,
        gather_evidence,
        run_csi_blocking,
    )

    if evidence_only:
        evidence = gather_evidence(cfg, container)
        console.print(_evidence_only_report(evidence))
        return
    report, _ = run_csi_blocking(cfg, container)
    console.print(report)


@app.command()
def digest(
    config: Path = CONFIG_OPT,
    days: int = typer.Option(
        0,
        "--days",
        help="Override digest.lookback_days for this run (0 = use config).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the digest body to stdout instead of sending it.",
    ),
    channel: list[str] = DIGEST_CHANNEL_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Build and send the weekly rollup. Useful for one-shot manual sends."""
    _setup_logging(verbose)
    cfg = load_config(config)
    from .digest import run_digest_blocking

    lookback = days if days > 0 else cfg.digest.lookback_days
    body, results, notes_path = run_digest_blocking(
        cfg,
        days=lookback,
        dry_run=dry_run,
        channels=channel or None,
    )
    if dry_run:
        console.print(body)
        return
    if notes_path:
        console.print(f"[dim]Wrote digest to[/dim] {notes_path}")
    if not results:
        console.print(
            "[yellow]No channels enabled — set `outputs.<name>.enabled: true` "
            "or pass `--channel <name>` to deliver this digest.[/yellow]"
        )
        return
    for ch, status in results.items():
        colour = "green" if status == "sent" else "yellow"
        console.print(f"[{colour}]{ch:<10}[/{colour}] {status}")


@app.command()
def serve(config: Path = CONFIG_OPT, verbose: bool = VERBOSE_OPT) -> None:
    """Run the web UI + background scheduler (long-running)."""
    _setup_logging(verbose)
    cfg = load_config(config)
    # Lazy import to keep CLI startup fast.
    from .web import run_web

    # Pass the config path through so the settings API knows where to write
    # the user overlay (`config.user.yaml`).
    run_web(cfg, cfg_path=config)


@app.command()
def version() -> None:
    console.print(__version__)


# ─── interview sub-app ──────────────────────────────────────────────────

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
        "pending",
        "--status",
        "-s",
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
        False, "--no-recurate", help="Just record the answer, don't regenerate the note."
    ),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Answer an interview question and re-curate the container's note."""
    _setup_logging(verbose)
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
        from .curator import Curator
        from .llm import LLMClient

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


# ─── watched sub-app ────────────────────────────────────────────────────

watched_app = typer.Typer(
    add_completion=False,
    help="Manage the list of GitHub/Codeberg repos the watched-repos plugin scans.",
    no_args_is_help=True,
)
app.add_typer(watched_app, name="watched")


@watched_app.command("add")
def watched_add(
    repo: str = typer.Argument(..., help="owner/name or codeberg.org/owner/name."),
    nickname: str = typer.Option(
        "", "--nickname", "-n", help="Friendly name used in notifications."
    ),
    version: str = typer.Option(
        "",
        "--version",
        help=(
            "Seed `current_version`. Leave empty for first-scan to seed from "
            "the latest release (avoids a phantom 0→N update on add)."
        ),
    ),
    config: Path = CONFIG_OPT,
) -> None:
    """Add a repo to the watched list (idempotent — re-adding revives it active)."""
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    try:
        wid = db.add_watched_repo(
            repo,
            nickname=nickname or None,
            current_version=version or None,
        )
        console.print(f"[green]Watching[/green] {repo} (id={wid})")
    finally:
        db.close()


@watched_app.command("list")
def watched_list(
    config: Path = CONFIG_OPT,
    all_: bool = typer.Option(
        False, "--all", help="Include inactive entries."
    ),
) -> None:
    """List watched repos."""
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    try:
        rows = db.list_watched_repos(active_only=not all_)
        if not rows:
            console.print("[dim]No watched repos.[/dim]")
            return
        table = Table(show_header=True, header_style="bold")
        table.add_column("ID", justify="right")
        table.add_column("Repo")
        table.add_column("Nickname")
        table.add_column("Current version")
        table.add_column("Active")
        for r in rows:
            table.add_row(
                str(r["id"]),
                r["repo"],
                r["nickname"] or "",
                r["current_version"] or "[dim](not seeded)[/dim]",
                "✓" if r["active"] else "·",
            )
        console.print(table)
    finally:
        db.close()


@watched_app.command("toggle")
def watched_toggle(
    watched_id: int = typer.Argument(..., help="ID from `homelabsage watched list`."),
    config: Path = CONFIG_OPT,
) -> None:
    """Flip a watched repo between active and inactive."""
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    try:
        row = db.get_watched_repo(watched_id)
        if row is None:
            console.print(f"[red]No watched repo with id={watched_id}.[/red]")
            raise typer.Exit(code=1)
        new_state = not bool(row["active"])
        db.toggle_watched_repo(watched_id, new_state)
        word = "activated" if new_state else "deactivated"
        console.print(f"[green]{row['repo']}[/green] {word}")
    finally:
        db.close()


@watched_app.command("remove")
def watched_remove(
    watched_id: int = typer.Argument(..., help="ID from `homelabsage watched list`."),
    config: Path = CONFIG_OPT,
) -> None:
    """Drop a watched repo entirely."""
    cfg = load_config(config)
    db = Database(cfg.storage.database_path)
    try:
        row = db.get_watched_repo(watched_id)
        if row is None:
            console.print(f"[red]No watched repo with id={watched_id}.[/red]")
            raise typer.Exit(code=1)
        db.remove_watched_repo(watched_id)
        console.print(f"[green]Removed[/green] {row['repo']}")
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


if __name__ == "__main__":
    app()
