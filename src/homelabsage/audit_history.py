"""Append-only audit history.

Each scan's audit findings get a one-row JSONL append into
`<notes_dir>/audit_history.jsonl`. That file is the truth source for
"what new findings appeared today?" — diff a fresh report against
the most recent row to see what's new vs what's persisted.

We never rewrite or compact this file. Trend analysis is read-side;
the writer is append-only so concurrent scans don't race.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


FILE_NAME = "audit_history.jsonl"


@dataclass(frozen=True)
class HistoryEntry:
    """One persisted snapshot."""

    generated_at: str
    finding_keys: frozenset[tuple[str, str, str]]
    counts_by_severity: dict[str, int]


def _fingerprint(finding: dict[str, Any]) -> tuple[str, str, str]:
    """Reduce a finding to a stable identity tuple.

    A finding's `(category, source_kind, source_ref)` is what makes
    one finding "the same as" another across runs. Severity may
    upgrade/downgrade between runs without us calling it new; the
    title/detail may rephrase. The tuple keeps the diff stable.
    """
    return (
        str(finding.get("category") or ""),
        str(finding.get("source_kind") or ""),
        str(finding.get("source_ref") or ""),
    )


def append(notes_dir: str | Path, report_json: dict[str, Any]) -> Path | None:
    """Append `report_json` as one JSONL row. Best-effort.

    Returns the path on success, None when notes_dir is empty / not
    writable. The caller passes `report.to_json()` so we don't
    re-derive the shape here.
    """
    if not notes_dir:
        return None
    path = Path(notes_dir).expanduser() / FILE_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(report_json) + "\n")
    except OSError as e:
        log.warning("audit_history: append failed: %s", e)
        return None
    return path


def load_latest(notes_dir: str | Path) -> HistoryEntry | None:
    """Read the LAST JSONL row. None when file missing / empty."""
    if not notes_dir:
        return None
    path = Path(notes_dir).expanduser() / FILE_NAME
    if not path.is_file():
        return None
    last_line: str | None = None
    try:
        # Tail-only is overkill for a small homelab DB — read+iterate.
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last_line = line
    except OSError as e:
        log.warning("audit_history: load failed: %s", e)
        return None
    if last_line is None:
        return None
    try:
        data = json.loads(last_line)
    except ValueError:
        return None
    findings = data.get("findings") or []
    if not isinstance(findings, list):
        return None
    keys = frozenset(
        _fingerprint(f) for f in findings if isinstance(f, dict)
    )
    counts = data.get("counts_by_severity") or {}
    if not isinstance(counts, dict):
        counts = {}
    return HistoryEntry(
        generated_at=str(data.get("generated_at") or ""),
        finding_keys=keys,
        counts_by_severity={str(k): int(v) for k, v in counts.items()},
    )


def diff_against_latest(
    notes_dir: str | Path,
    report_json: dict[str, Any],
) -> dict[str, Any]:
    """Return new / resolved keys vs the latest persisted snapshot.

    `new` — findings in this report whose fingerprint isn't in the
    previous snapshot.
    `resolved` — fingerprints in the previous snapshot that are
    absent from this report.
    Returns empty lists when no previous snapshot exists.
    """
    prev = load_latest(notes_dir)
    current_findings = report_json.get("findings") or []
    if not isinstance(current_findings, list):
        current_findings = []
    current_keys = {
        _fingerprint(f): f for f in current_findings if isinstance(f, dict)
    }
    if prev is None:
        return {
            "previous_snapshot": None,
            "new": list(current_findings),
            "resolved": [],
        }
    new_keys = set(current_keys) - prev.finding_keys
    resolved_keys = prev.finding_keys - set(current_keys)
    return {
        "previous_snapshot": prev.generated_at,
        "new": [current_keys[k] for k in sorted(new_keys)],
        "resolved": [
            {"category": k[0], "source_kind": k[1], "source_ref": k[2]}
            for k in sorted(resolved_keys)
        ],
    }


def list_history(
    notes_dir: str | Path,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Return up to `limit` snapshot summaries, newest first.

    Each row is the compact `{generated_at, counts_by_severity,
    counts_by_category, finding_count, healthy}` shape — never the
    full findings list (a dashboard polling history of a busy
    homelab would otherwise download megabytes per request). For
    the full per-snapshot detail, the caller derives it from the
    JSONL directly.

    Pagination: `offset` skips that many rows from the newest end.
    """
    if not notes_dir:
        return []
    path = Path(notes_dir).expanduser() / FILE_NAME
    if not path.is_file():
        return []
    try:
        # Read every row, take the LAST `limit+offset` in file order
        # (oldest→newest), then reverse to return newest first. The
        # JSONL is small enough that streaming-from-end isn't worth
        # the complexity for typical homelab scan cadences.
        lines: list[str] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    lines.append(line)
    except OSError as e:
        log.warning("audit_history: list failed: %s", e)
        return []
    # newest-first
    lines.reverse()
    window = lines[offset:offset + max(1, limit)]
    out: list[dict[str, Any]] = []
    for raw in window:
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        findings = data.get("findings") or []
        out.append({
            "generated_at": str(data.get("generated_at") or ""),
            "healthy": bool(data.get("healthy")),
            "counts_by_severity": data.get("counts_by_severity") or {},
            "counts_by_category": data.get("counts_by_category") or {},
            "finding_count": len(findings) if isinstance(findings, list) else 0,
        })
    return out


__all__ = [
    "HistoryEntry",
    "append",
    "diff_against_latest",
    "list_history",
    "load_latest",
]
