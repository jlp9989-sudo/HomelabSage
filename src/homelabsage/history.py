"""Update-history CSV export.

Why: the SQLite `updates` table is the project's audit log. Most users
will never SELECT against it directly, but they ARE comfortable with
spreadsheets. `homelabsage history --csv` dumps every row to CSV the
user can pivot, sort and filter at will.

Columns are chosen for usefulness over completeness:

  id, source, subject, current_version, new_version, severity, status,
  detected_at, analyzed_at, release_url, summary, breaking_changes,
  recommended_action

`breaking_changes` is joined with ` | ` so the cell stays single-line.
`context` is intentionally omitted — it's a JSON blob with many shapes
across detectors and pasting it into a spreadsheet just produces noise.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from .db import Database
from .models import AnalyzedUpdate

COLUMNS: tuple[str, ...] = (
    "id",
    "source",
    "subject",
    "current_version",
    "new_version",
    "severity",
    "status",
    "detected_at",
    "analyzed_at",
    "release_url",
    "summary",
    "breaking_changes",
    "recommended_action",
)


def _row(item: AnalyzedUpdate) -> dict[str, str]:
    a = item.analysis
    return {
        "id": item.id,
        "source": item.update.source,
        "subject": item.update.subject,
        "current_version": item.update.current_version,
        "new_version": item.update.new_version,
        "severity": a.severity.value if a else "",
        "status": item.status.value,
        "detected_at": item.detected_at.isoformat() if item.detected_at else "",
        "analyzed_at": item.analyzed_at.isoformat() if item.analyzed_at else "",
        "release_url": item.update.release_url or "",
        "summary": a.summary if a else "",
        "breaking_changes": " | ".join(a.breaking_changes) if a else "",
        "recommended_action": (a.recommended_action or "") if a else "",
    }


def write_csv(db: Database, output: Path | str, *, limit: int = 10000) -> int:
    """Write the history to `output` (path or `-` for stdout-as-string).

    Returns the number of rows written. `limit` caps the read; default
    covers years of homelab scans without blowing memory.
    """
    items = db.list(limit=limit)
    if str(output) == "-":
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(COLUMNS), extrasaction="ignore")
        w.writeheader()
        n = 0
        for it in items:
            w.writerow(_row(it))
            n += 1
        # Caller (the CLI) is responsible for printing; for tests we
        # raise the buffer contents so it can be asserted on.
        return n  # The caller path that uses `-` reads stdout separately.
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(COLUMNS), extrasaction="ignore")
        w.writeheader()
        n = 0
        for it in items:
            w.writerow(_row(it))
            n += 1
    return n


def dump_to_string(db: Database, *, limit: int = 10000) -> str:
    """Convenience for tests + the CLI's stdout path."""
    items = db.list(limit=limit)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(COLUMNS), extrasaction="ignore")
    w.writeheader()
    for it in items:
        w.writerow(_row(it))
    return buf.getvalue()


__all__ = ["COLUMNS", "dump_to_string", "write_csv"]
