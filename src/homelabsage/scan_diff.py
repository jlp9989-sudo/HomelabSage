"""Compare two history.csv dumps to see what changed between scans.

The user case: "I exported last week, exported again today — what
moved?". Pure-text-in, pure-text-out — never touches the live DB.

Output sections:
  - **added**: ids present in `new` but not in `old`
  - **removed**: ids present in `old` but not in `new`
  - **status_changed**: same id, status field moved (`new` → `analyzed`,
    `analyzed` → `applied`, etc).
  - **severity_changed**: same id, severity field moved.

Stable id is the CSV's `id` column — `{source}:{subject}:{new_version}`
in HomelabSage's convention. Matching on the id keeps the diff
meaningful even when columns are reordered.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass


@dataclass
class ScanDiff:
    """All four categories. Each row is the new-side dict (or the
    old-side dict for removed rows)."""

    added: list[dict[str, str]]
    removed: list[dict[str, str]]
    status_changed: list[dict[str, str]]
    severity_changed: list[dict[str, str]]

    def empty(self) -> bool:
        return not (
            self.added or self.removed
            or self.status_changed or self.severity_changed
        )

    def counts(self) -> dict[str, int]:
        return {
            "added": len(self.added),
            "removed": len(self.removed),
            "status_changed": len(self.status_changed),
            "severity_changed": len(self.severity_changed),
        }


def _load(text: str) -> dict[str, dict[str, str]]:
    """Parse a CSV body into `{id: row_dict}`. Rows without an id
    are dropped — they can't be compared anyway."""
    reader = csv.DictReader(io.StringIO(text))
    out: dict[str, dict[str, str]] = {}
    for row in reader:
        uid = row.get("id") or ""
        if not uid:
            continue
        out[uid] = row
    return out


def diff(old_csv: str, new_csv: str) -> ScanDiff:
    """Compute the four-way diff between two CSV bodies."""
    old = _load(old_csv)
    new = _load(new_csv)
    old_ids = set(old)
    new_ids = set(new)

    added = [new[uid] for uid in sorted(new_ids - old_ids)]
    removed = [old[uid] for uid in sorted(old_ids - new_ids)]
    status_changed: list[dict[str, str]] = []
    severity_changed: list[dict[str, str]] = []
    for uid in sorted(old_ids & new_ids):
        prev = old[uid]
        curr = new[uid]
        if prev.get("status", "") != curr.get("status", ""):
            curr_with_prev = dict(curr)
            curr_with_prev["_previous_status"] = prev.get("status", "")
            status_changed.append(curr_with_prev)
        if prev.get("severity", "") != curr.get("severity", ""):
            curr_with_prev = dict(curr)
            curr_with_prev["_previous_severity"] = prev.get("severity", "")
            severity_changed.append(curr_with_prev)
    return ScanDiff(
        added=added,
        removed=removed,
        status_changed=status_changed,
        severity_changed=severity_changed,
    )


def render_markdown(d: ScanDiff) -> str:
    """Render the diff as a Markdown report."""
    if d.empty():
        return "# Scan diff\n\n_No changes between the two snapshots._\n"
    counts = d.counts()
    pills = ", ".join(f"{n} {k}" for k, n in counts.items() if n)
    lines = ["# Scan diff", "", f"**Changes:** {pills}", ""]
    if d.added:
        lines.append("## Added")
        for r in d.added:
            lines.append(f"- `{r.get('id', '')}` — {r.get('subject', '')} "
                         f"({r.get('current_version', '')} → "
                         f"{r.get('new_version', '')}) "
                         f"severity={r.get('severity', '')}")
        lines.append("")
    if d.removed:
        lines.append("## Removed")
        for r in d.removed:
            lines.append(f"- `{r.get('id', '')}` — {r.get('subject', '')}")
        lines.append("")
    if d.status_changed:
        lines.append("## Status changed")
        for r in d.status_changed:
            lines.append(
                f"- `{r.get('id', '')}` — "
                f"{r.get('_previous_status', '')} → {r.get('status', '')}"
            )
        lines.append("")
    if d.severity_changed:
        lines.append("## Severity changed")
        for r in d.severity_changed:
            lines.append(
                f"- `{r.get('id', '')}` — "
                f"{r.get('_previous_severity', '')} → {r.get('severity', '')}"
            )
        lines.append("")
    return "\n".join(lines)


__all__ = ["ScanDiff", "diff", "render_markdown"]
