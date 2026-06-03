"""Server chronicle — narrative Markdown of major homelab changes.

While the weekly digest is a snapshot, the chronicle is a timeline:
"what mattered in the last N days, written like a logbook". Different
audience too — the digest is for the user's phone, the chronicle is
for `notes/chronicle.md` (or a quarterly recap email).

We intentionally surface only EVENTS that left a trace:
  - Updates marked APPLIED (the user took action)
  - Updates marked DISMISSED with notes (the user said no, and why
    matters for future reasoning)
  - Updates that were HOLD / BREAKING — the curator's incremental hook
    already pinned those to per-service notes; the chronicle threads
    them into one chronological story.
  - Verdict severity changes for the same service across the window
    (e.g. went from `info` → `critical` after a CVE adapter run).

The body is plain Markdown — no LLM in the loop. The analyzer already
ran on each row; the chronicle's job is to thread, not to interpret.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from ._time import parse_iso, utcnow
from .db import Database
from .models import UpdateStatus

log = logging.getLogger(__name__)


_SEVERITY_EMOJI: dict[str, str] = {
    "critical": "🔴", "high": "🟠", "medium": "🟡", "info": "🔵",
}


@dataclass
class ChronicleEntry:
    """One line in the timeline."""

    when: datetime
    subject: str
    kind: str        # applied | dismissed | breaking | hold | severity_jump
    headline: str    # short bold line
    detail: str      # 1-2 sentences


@dataclass
class Chronicle:
    period_start: datetime
    period_end: datetime
    entries: list[ChronicleEntry] = field(default_factory=list)

    def counts_by_kind(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for e in self.entries:
            c[e.kind] = c.get(e.kind, 0) + 1
        return c


def _normalise_now(now: datetime | None) -> datetime:
    if now is None:
        return utcnow()
    if now.tzinfo is None:
        from datetime import UTC
        return now.replace(tzinfo=UTC)
    return now


def build_chronicle(
    db: Database,
    *,
    days: int = 30,
    now: datetime | None = None,
) -> Chronicle:
    """Walk the last `days` of updates and produce a narrative timeline.

    The window is inclusive of both ends — items detected exactly on the
    cutoff are kept (boundary races shouldn't drop history).
    """
    end = _normalise_now(now)
    start = end - timedelta(days=days)

    # Pull a generous page; the DB orders by detected_at DESC. 1000
    # covers ~6 months on a busy homelab.
    items = [
        it for it in db.list(limit=1000)
        if it.detected_at and it.detected_at >= start
    ]

    # Severity history per subject — drives the severity_jump signal.
    severity_per_subject: dict[str, list[tuple[datetime, str]]] = {}
    for it in items:
        if it.analysis is None:
            continue
        sev = it.analysis.severity.value
        severity_per_subject.setdefault(it.update.subject, []).append(
            (it.detected_at, sev)
        )

    entries: list[ChronicleEntry] = []
    for it in items:
        u = it.update
        a = it.analysis
        at = it.analyzed_at or it.detected_at

        if it.status == UpdateStatus.APPLIED:
            entries.append(ChronicleEntry(
                when=at,
                subject=u.subject,
                kind="applied",
                headline=f"Applied {u.subject} {u.current_version} → {u.new_version}",
                detail=(a.summary if a else "(no analysis)"),
            ))
            continue

        if it.status == UpdateStatus.DISMISSED:
            entries.append(ChronicleEntry(
                when=at,
                subject=u.subject,
                kind="dismissed",
                headline=f"Dismissed update for {u.subject} ({u.new_version})",
                detail=(a.summary if a else "(no analysis)"),
            ))
            continue

        if a is not None and a.breaking_changes:
            top = a.breaking_changes[0]
            entries.append(ChronicleEntry(
                when=at,
                subject=u.subject,
                kind="breaking",
                headline=f"Breaking change flagged on {u.subject} {u.new_version}",
                detail=f"{top} ({len(a.breaking_changes)} total breaking changes listed).",
            ))
            continue

        # 'hold' verdicts have an analysis whose recommended_action contains
        # "hold" (the prompt's exact phrasing for the orphan/abandoned cases).
        if a is not None and a.recommended_action and "hold" in a.recommended_action.lower():
            entries.append(ChronicleEntry(
                when=at,
                subject=u.subject,
                kind="hold",
                headline=f"Hold recommended on {u.subject}",
                detail=a.recommended_action,
            ))
            continue

    # Severity jumps — only when severity strictly increased over the window
    # for the same subject (more than one analysed point).
    sev_order = {"info": 0, "medium": 1, "high": 2, "critical": 3}
    for subject, history in severity_per_subject.items():
        if len(history) < 2:
            continue
        history.sort(key=lambda kv: kv[0])
        first_sev = history[0][1]
        last_dt, last_sev = history[-1]
        if sev_order[last_sev] > sev_order[first_sev]:
            entries.append(ChronicleEntry(
                when=last_dt,
                subject=subject,
                kind="severity_jump",
                headline=f"Severity escalated on {subject}: {first_sev} → {last_sev}",
                detail=f"Across {len(history)} analyses in the chronicle window.",
            ))

    # Newest first reads like a logbook.
    entries.sort(key=lambda e: e.when, reverse=True)
    return Chronicle(period_start=start, period_end=end, entries=entries)


def render_markdown(chronicle: Chronicle) -> str:
    """Render the chronicle as one Markdown body — `notes/chronicle.md`."""
    period = (
        f"{chronicle.period_start.strftime('%Y-%m-%d')} → "
        f"{chronicle.period_end.strftime('%Y-%m-%d')}"
    )
    lines: list[str] = [
        f"# Homelab chronicle — {chronicle.period_end.strftime('%Y-%m-%d')}",
        f"_Window: {period} "
        f"({(chronicle.period_end - chronicle.period_start).days} days)_",
        "",
    ]
    if not chronicle.entries:
        lines.append("Nothing of note this window. Quiet logs are a good sign.")
        return "\n".join(lines)

    counts = chronicle.counts_by_kind()
    pills = ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
    lines.append(f"**Events:** {pills}")
    lines.append("")

    # Section per kind, in narrative order: applied → dismissed →
    # breaking → hold → severity_jump.
    order = ["applied", "dismissed", "breaking", "hold", "severity_jump"]
    by_kind: dict[str, list[ChronicleEntry]] = {k: [] for k in order}
    for e in chronicle.entries:
        by_kind.setdefault(e.kind, []).append(e)

    section_titles = {
        "applied": "Applied",
        "dismissed": "Dismissed",
        "breaking": "Breaking changes flagged",
        "hold": "Hold-recommended",
        "severity_jump": "Severity escalations",
    }
    for kind in order:
        section = by_kind.get(kind) or []
        if not section:
            continue
        lines.append("")
        lines.append(f"## {section_titles[kind]}")
        for e in section:
            stamp = e.when.strftime("%Y-%m-%d")
            lines.append(f"- **{stamp}** — {e.headline}")
            lines.append(f"  - {e.detail}")
    return "\n".join(lines)


def write_to_notes(notes_dir: str | Path, body: str) -> Path | None:
    if not notes_dir:
        return None
    out_dir = Path(notes_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("chronicle notes_dir mkdir failed: %s", e)
        return None
    target = out_dir / "chronicle.md"
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(body + "\n", encoding="utf-8")
    tmp.replace(target)
    return target


def run_chronicle(
    db: Database,
    notes_dir: str | None,
    *,
    days: int = 30,
) -> tuple[Chronicle, Path | None]:
    """End-to-end: build → render → (optionally) write to notes_dir."""
    chronicle = build_chronicle(db, days=days)
    body = render_markdown(chronicle)
    path = write_to_notes(notes_dir, body) if notes_dir else None
    return chronicle, path


# parse_iso is re-exported here only so tests can mirror the same
# tz-aware semantics other modules use without depending on _time directly.
__all__ = [
    "Chronicle", "ChronicleEntry",
    "build_chronicle", "render_markdown", "write_to_notes", "run_chronicle",
    "parse_iso",
]
