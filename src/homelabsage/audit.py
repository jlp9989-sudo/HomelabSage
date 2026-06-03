"""Proactive auditor — synthesises every detector into one health report.

A non-technical user won't read 40 Notion rows; they want to know "is
my homelab healthy?". This module collects every signal the detectors
already attach to `Update.context` (repo_health, alternatives,
orphan_since_days, cve, cascade, image_size_growth, puid_pgid) plus the
parity gate state and the pending-dispatches queue, and renders one
prioritised Markdown report.

Hard rule: **every line of the report cites the concrete row, repo or
note it came from**. We never invent recommendations. If the data
doesn't support a line, the line isn't there. This is the single most
important property of the auditor — the whole point is to make existing
signals legible, not to add a new layer of "AI says so".

Output sinks:
  - `notes/audit.md` — picked up by the curator on the next pass
    (recursive context wins).
  - `/audit` web route — read-only HTML view.
  - `/api/audit` — JSON shape for scripting + MCP.

The body is deterministic Markdown — no LLM in the loop here. The
analyzer's verdicts already went through the LLM; the auditor just
sorts and surfaces them. Adding an LLM pass would invite paraphrase
hallucinations on facts that are already concrete.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._time import utcnow
from .config import Config
from .db import Database
from .models import AnalyzedUpdate, UpdateStatus
from .parity import is_parity_running

log = logging.getLogger(__name__)


_SEVERITY_EMOJI: dict[str, str] = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "info": "🔵",
}


@dataclass
class AuditFinding:
    """One row in the audit. Always cites a concrete source.

    `source_kind` is a short tag (`cve`, `abandoned`, `orphan`,
    `alternative`, `bloatware`, `pending_push`, `parity`). `source_ref`
    points back at the originating Update.id (or 'parity'/'queue' for
    state-level findings). The renderer puts both in the rendered line
    so the reader can verify the claim.
    """

    severity: str               # critical | high | medium | info
    category: str               # short tag for filtering
    title: str                  # one-liner heading
    detail: str                 # 1-2 sentence body
    source_kind: str            # cve / abandoned / orphan / alternative / …
    source_ref: str             # update_id or sentinel
    cite: str                   # the literal evidence: a tag, a URL, a count


@dataclass
class AuditReport:
    """Frozen snapshot. Renderable to Markdown or JSON."""

    generated_at: str
    findings: list[AuditFinding] = field(default_factory=list)
    counts_by_category: dict[str, int] = field(default_factory=dict)
    counts_by_severity: dict[str, int] = field(default_factory=dict)
    healthy: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "healthy": self.healthy,
            "counts_by_category": self.counts_by_category,
            "counts_by_severity": self.counts_by_severity,
            "findings": [
                {
                    "severity": f.severity,
                    "category": f.category,
                    "title": f.title,
                    "detail": f.detail,
                    "source_kind": f.source_kind,
                    "source_ref": f.source_ref,
                    "cite": f.cite,
                }
                for f in self.findings
            ],
        }


# ─── per-signal extractors ──────────────────────────────────────────────


def _cve_findings(item: AnalyzedUpdate) -> list[AuditFinding]:
    """Surface critical/high CVE counts attached by the docker plugin."""
    ctx = item.update.context or {}
    cve = ctx.get("cve")
    if not isinstance(cve, dict):
        return []
    counts = cve.get("counts") or {}
    crit = int(counts.get("critical") or 0)
    high = int(counts.get("high") or 0)
    out: list[AuditFinding] = []
    if crit:
        ids = (cve.get("top_critical") or [])[:3]
        out.append(AuditFinding(
            severity="critical", category="cve",
            title=f"{item.update.subject} has {crit} critical CVE(s)",
            detail=(
                f"Update {item.update.current_version} → "
                f"{item.update.new_version}. Top: {', '.join(ids) if ids else '(no IDs reported)'}."
            ),
            source_kind="cve", source_ref=item.id,
            cite=f"counts.critical={crit}",
        ))
    elif high:
        ids = (cve.get("top_high") or [])[:3]
        out.append(AuditFinding(
            severity="high", category="cve",
            title=f"{item.update.subject} has {high} high-severity CVE(s)",
            detail=(
                f"Top: {', '.join(ids) if ids else '(no IDs reported)'}."
            ),
            source_kind="cve", source_ref=item.id,
            cite=f"counts.high={high}",
        ))
    return out


def _abandoned_findings(item: AnalyzedUpdate) -> list[AuditFinding]:
    """`repo_health.status == 'abandoned'` signals from the docker plugin."""
    ctx = item.update.context or {}
    rh = ctx.get("repo_health")
    if not isinstance(rh, dict) or rh.get("status") != "abandoned":
        return []
    reason = rh.get("reason") or "no recent push"
    return [AuditFinding(
        severity="high", category="abandoned",
        title=f"{item.update.subject} runs on an abandoned upstream",
        detail=(
            f"Upstream marked abandoned ({reason}); consider evaluating a "
            f"maintained fork before next upgrade."
        ),
        source_kind="repo_health", source_ref=item.id,
        cite=f"repo_health.status=abandoned ({reason})",
    )]


def _orphan_findings(item: AnalyzedUpdate) -> list[AuditFinding]:
    """`orphan_since_days >= 30` containers — long-stopped + pending update."""
    ctx = item.update.context or {}
    days = ctx.get("orphan_since_days")
    if not isinstance(days, int) or days < 30:
        return []
    return [AuditFinding(
        severity="medium", category="orphan",
        title=f"{item.update.subject} has been stopped for {days} days",
        detail="Decide whether to remove instead of upgrading.",
        source_kind="orphan", source_ref=item.id,
        cite=f"orphan_since_days={days}",
    )]


def _alternative_findings(item: AnalyzedUpdate) -> list[AuditFinding]:
    """At most ONE alternative per row — matches the prompt rule."""
    ctx = item.update.context or {}
    alts = ctx.get("alternatives") or []
    if not alts:
        return []
    top = alts[0]
    image = top.get("image") or "(unknown)"
    return [AuditFinding(
        severity="info", category="alternative",
        title=f"Better-maintained alternative exists for {item.update.subject}",
        detail=f"Consider `{image}` — same purpose, higher adoption.",
        source_kind="alternative", source_ref=item.id,
        cite=f"alternatives[0].image={image}",
    )]


def _tag_lag_findings(
    item: AnalyzedUpdate,
    *,
    warn_after_days: int,
    critical_after_days: int,
) -> list[AuditFinding]:
    """Floating-tag containers where the registry moved but the local
    image didn't."""
    ctx = item.update.context or {}
    if "remote_pushed_at" not in ctx and "local_pulled_at" not in ctx:
        return []
    from .tag_lag import derive_tag_lag
    lag = derive_tag_lag(
        ctx,
        warn_after_days=warn_after_days,
        critical_after_days=critical_after_days,
    )
    if lag is None or lag.severity == "info":
        return []
    return [AuditFinding(
        severity=lag.severity, category="tag_lag",
        title=(
            f"{item.update.subject} is {lag.days_local_behind} day(s) "
            f"behind the registry"
        ),
        detail=(
            f"The floating tag has been bumped upstream {lag.days_since_remote_push} "
            f"day(s) ago. Pull a fresh image to catch up."
        ),
        source_kind="tag_lag", source_ref=item.id,
        cite=f"days_local_behind={lag.days_local_behind}",
    )]


def _container_age_findings(item: AnalyzedUpdate) -> list[AuditFinding]:
    """Containers running past their threshold without recreation."""
    ctx = item.update.context or {}
    age = ctx.get("container_age")
    if not isinstance(age, dict):
        return []
    days = int(age.get("days_old") or 0)
    if days < 365:
        sev = "info"
    elif days < 730:
        sev = "medium"
    else:
        sev = "high"
    return [AuditFinding(
        severity=sev, category="container_age",
        title=f"{item.update.subject} hasn't been recreated in {days} days",
        detail=(
            "Long-running containers accumulate config drift. Consider "
            "`docker compose down && docker compose up -d` to refresh."
        ),
        source_kind="container_age", source_ref=item.id,
        cite=f"days_old={days}",
    )]


def _bloatware_findings(item: AnalyzedUpdate) -> list[AuditFinding]:
    """`image_size_growth.triggered=true` from the docker plugin."""
    ctx = item.update.context or {}
    growth = ctx.get("image_size_growth")
    if not isinstance(growth, dict) or not growth.get("triggered"):
        return []
    return [AuditFinding(
        severity="info", category="bloatware",
        title=f"{item.update.subject} image grew {growth.get('ratio')}×",
        detail=(
            f"{growth.get('current_mib')} MiB → {growth.get('new_mib')} MiB. "
            f"Investigate before applying if storage-constrained."
        ),
        source_kind="image_size_growth", source_ref=item.id,
        cite=f"image_size_growth.ratio={growth.get('ratio')}",
    )]


def _pending_dispatch_findings(rows: list[dict]) -> list[AuditFinding]:
    """Queued push notifications waiting on parity gate clear."""
    if not rows:
        return []
    by_output: dict[str, int] = {}
    for r in rows:
        by_output[r["output_id"]] = by_output.get(r["output_id"], 0) + 1
    detail = ", ".join(f"{n} via {o}" for o, n in sorted(by_output.items()))
    return [AuditFinding(
        severity="medium", category="pending_push",
        title=f"{len(rows)} push notification(s) queued by the parity gate",
        detail=f"Breakdown: {detail}. They will fire on the next ungated scan.",
        source_kind="pending_dispatches", source_ref="queue",
        cite=f"pending_dispatches.count={len(rows)}",
    )]


def _parity_findings(parity_active: bool, reason: str) -> list[AuditFinding]:
    if not parity_active:
        return []
    return [AuditFinding(
        severity="info", category="parity",
        title="Parity check / resync is currently running",
        detail=(
            f"{reason}. Push notifications are gated until it clears. "
            f"The weekly digest still fires as a backstop."
        ),
        source_kind="parity", source_ref="probe",
        cite=reason,
    )]


def _backup_health_findings(report: object) -> list[AuditFinding]:
    """Surface backup-staleness probe results.

    `report` is a `BackupHealthReport`-shaped object (duck-typed so the
    auditor's import surface stays narrow). One finding per repo that
    failed or crossed a threshold; healthy repos stay silent.
    """
    if report is None:
        return []
    findings: list[AuditFinding] = []
    for r in getattr(report, "results", []) or []:
        if r.ok and r.severity in ("info",):
            continue
        if not r.ok:
            findings.append(AuditFinding(
                severity=r.severity, category="backup_health",
                title=f"Backup repo `{r.repo_name}` probe failed",
                detail=r.reason,
                source_kind="backup_health", source_ref=f"backup:{r.repo_name}",
                cite=f"{r.tool}: {r.reason}",
            ))
            continue
        when = r.latest_snapshot_at.strftime("%Y-%m-%d %H:%M") if r.latest_snapshot_at else "?"
        findings.append(AuditFinding(
            severity=r.severity, category="backup_health",
            title=f"Backup repo `{r.repo_name}` is {r.staleness_days} day(s) stale",
            detail=(
                f"Last {r.tool} snapshot at {when}; total snapshots in repo: "
                f"{r.snapshot_count}."
            ),
            source_kind="backup_health", source_ref=f"backup:{r.repo_name}",
            cite=f"staleness_days={r.staleness_days}",
        ))
    return findings


def _health_check_findings(rows: list[dict]) -> list[AuditFinding]:
    """One finding per failed post-update health check row."""
    if not rows:
        return []
    findings: list[AuditFinding] = []
    for row in rows:
        if row.get("ok"):
            continue
        signal = row.get("signal") or "unknown"
        excerpt = (row.get("excerpt") or "")[:140]
        update_id = row.get("update_id") or "?"
        findings.append(AuditFinding(
            severity="high", category="post_update_regression",
            title=f"Post-update probe flagged `{row.get('container_name')}`",
            detail=f"Signal: `{signal}`. Log excerpt: `{excerpt or '(empty)'}`.",
            source_kind="health_check", source_ref=update_id,
            cite=f"signal={signal}",
        ))
    return findings


def _compose_lint_findings(report: object) -> list[AuditFinding]:
    """Surface compose-lint findings as auditor entries.

    Each compose smell becomes one row tagged `category=compose_lint`
    with the rule name in `source_kind`. Source-ref carries the
    file path so the user can grep for it locally.
    """
    if report is None:
        return []
    findings: list[AuditFinding] = []
    for f in getattr(report, "findings", []) or []:
        findings.append(AuditFinding(
            severity=f.severity, category="compose_lint",
            title=f"{f.rule} on {f.service}",
            detail=f.detail,
            source_kind=f.rule,
            source_ref=str(f.file),
            cite=f"{f.rule}={f.service}",
        ))
    return findings


def _log_anomaly_findings(rows: list[dict]) -> list[AuditFinding]:
    """One finding per container whose recent error rate breached its baseline."""
    if not rows:
        return []
    findings: list[AuditFinding] = []
    for row in rows:
        sigma = row.get("sigma") or 0
        rate = row.get("error_rate") or 0
        baseline = row.get("baseline_mean") or 0
        findings.append(AuditFinding(
            severity="high" if sigma >= 5 else "medium",
            category="log_anomaly",
            title=f"`{row.get('container_name')}` log error-rate spiked",
            detail=(
                f"Recent error/min={rate:.2f} vs baseline {baseline:.2f} "
                f"(σ={sigma:.1f})."
            ),
            source_kind="log_anomaly", source_ref=row.get("container_name") or "?",
            cite=f"sigma={sigma:.1f}",
        ))
    return findings


# ─── orchestrator ───────────────────────────────────────────────────────


_SEVERITY_ORDER = {"critical": 3, "high": 2, "medium": 1, "info": 0}


def build_report(
    cfg: Config, db: Database, *, limit: int = 500,
    backup_report: object | None = None,
) -> AuditReport:
    """Walk every active signal in the DB + live state probes.

    `limit` caps how many updates we read from the DB. 500 covers a
    busy month of scans; raise it if you batch-import historic data.

    `backup_report` is passed in by the engine (which probes once per scan
    and reuses the result here + in the analyzer context) — when None,
    the auditor runs its own probe lazily so the CLI works standalone.
    """
    findings: list[AuditFinding] = []
    items = db.list(limit=limit)
    for it in items:
        # Audit IGNORES applied / dismissed updates — those are decisions
        # already made, surfacing them again is noise.
        if it.status in (UpdateStatus.APPLIED, UpdateStatus.DISMISSED):
            continue
        findings.extend(_cve_findings(it))
        findings.extend(_abandoned_findings(it))
        findings.extend(_orphan_findings(it))
        findings.extend(_alternative_findings(it))
        findings.extend(_bloatware_findings(it))
        findings.extend(_container_age_findings(it))
        if cfg.tag_lag.enabled:
            findings.extend(_tag_lag_findings(
                it,
                warn_after_days=cfg.tag_lag.warn_after_days,
                critical_after_days=cfg.tag_lag.critical_after_days,
            ))

    # State-level signals — not per-update.
    findings.extend(_pending_dispatch_findings(db.list_pending_dispatches()))

    parity_active, parity_reason = False, ""
    if cfg.parity_gate.enabled:
        state = is_parity_running(mdstat_path=cfg.parity_gate.mdstat_path)
        parity_active, parity_reason = state.running, state.reason
    findings.extend(_parity_findings(parity_active, parity_reason))

    # Backup health: prefer the engine-provided cached report; fall back
    # to a fresh probe so `homelabsage audit` works without a scan.
    if backup_report is None and cfg.backup_health.enabled and cfg.backup_health.repos:
        from .backup_health import BackupHealthReport, probe_repo
        results = []
        for repo in cfg.backup_health.repos:
            results.append(probe_repo(
                name=repo.name, tool=repo.tool, env=repo.env,
                binary=(repo.binary or None),
                warn_after_days=repo.warn_after_days,
                critical_after_days=repo.critical_after_days,
                timeout=float(repo.timeout_seconds),
            ))
        backup_report = BackupHealthReport(results=results)
    findings.extend(_backup_health_findings(backup_report))

    # Post-update regressions + log anomalies — read from their tables when
    # the DB exposes them (no-op when the mixins aren't wired in).
    if hasattr(db, "list_recent_health_checks"):
        findings.extend(_health_check_findings(db.list_recent_health_checks(limit=200)))
    if hasattr(db, "list_recent_log_anomalies"):
        findings.extend(_log_anomaly_findings(db.list_recent_log_anomalies(limit=200)))

    # Compose linter — same scan paths as the cascade detector so the
    # user doesn't configure two lists. No-op when paths empty.
    if cfg.sources.docker.compose_scan_paths and cfg.compose_lint.enabled:
        from .compose_lint import lint_paths
        lint_report = lint_paths(cfg.sources.docker.compose_scan_paths)
        findings.extend(_compose_lint_findings(lint_report))

    # Severity first (critical → info), then category alphabetical so the
    # same input always produces the same output.
    findings.sort(key=lambda f: (-_SEVERITY_ORDER[f.severity], f.category, f.title))

    by_cat: dict[str, int] = {}
    by_sev: dict[str, int] = {}
    for f in findings:
        by_cat[f.category] = by_cat.get(f.category, 0) + 1
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1

    return AuditReport(
        generated_at=utcnow().isoformat(),
        findings=findings,
        counts_by_category=by_cat,
        counts_by_severity=by_sev,
        healthy=(not findings),
    )


def render_markdown(report: AuditReport) -> str:
    """Render the report as a single Markdown body for `notes/audit.md`."""
    lines: list[str] = [
        f"# Homelab audit — {report.generated_at}",
        "",
    ]
    if report.healthy:
        lines.append("All clear. No actionable findings.")
        return "\n".join(lines)

    sev_parts = []
    for sev in ("critical", "high", "medium", "info"):
        n = report.counts_by_severity.get(sev, 0)
        if n:
            sev_parts.append(f"{_SEVERITY_EMOJI[sev]} {n} {sev}")
    if sev_parts:
        lines.append("**Severity:** " + " · ".join(sev_parts))
    if report.counts_by_category:
        cat_parts = [f"{n} {c}" for c, n in sorted(report.counts_by_category.items())]
        lines.append("**By category:** " + ", ".join(cat_parts))
    lines.append("")

    current_sev: str | None = None
    for f in report.findings:
        if f.severity != current_sev:
            lines.append("")
            lines.append(f"## {_SEVERITY_EMOJI[f.severity]} {f.severity.upper()}")
            current_sev = f.severity
        lines.append(f"- **{f.title}**")
        lines.append(f"  - {f.detail}")
        lines.append(f"  - _source:_ `{f.source_kind}` on `{f.source_ref}` — `{f.cite}`")
    return "\n".join(lines)


def write_to_notes(notes_dir: str | Path, body: str) -> Path | None:
    """Atomic write of `audit.md` into the curator's notes dir."""
    if not notes_dir:
        return None
    out_dir = Path(notes_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("audit notes_dir mkdir failed: %s", e)
        return None
    target = out_dir / "audit.md"
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(body + "\n", encoding="utf-8")
    tmp.replace(target)
    return target


def run_audit(cfg: Config, db: Database) -> tuple[AuditReport, Path | None]:
    """End-to-end: build + render + (optionally) write to notes_dir."""
    report = build_report(cfg, db)
    body = render_markdown(report)
    notes_path = write_to_notes(cfg.notes.notes_dir, body) if cfg.notes.notes_dir else None
    return report, notes_path
