"""Container-disappearance tracker.

When a container is removed (not just stopped) between scans, the
docker plugin simply doesn't enumerate it anymore. The user usually
notices weeks later: "wait, where did mealie-postgres go?". This
module persists the set of container names we saw on the last scan
and reports the delta on the next.

Designed as a pure function pair: `diff_snapshots(prev, current)`
returns the disappeared names; the auditor / engine wires the
persistence (we store the snapshot in a tiny JSON file under
`storage.database_path`'s parent dir, NOT a new SQL table — the
snapshot is single-row state, not append-only history).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class DisappearanceFinding:
    """One container missing from the current scan."""

    name: str
    last_seen_at: str   # ISO 8601 string from prev snapshot

    def to_context(self) -> dict:
        return {"name": self.name, "last_seen_at": self.last_seen_at}


def diff_snapshots(
    prev: dict[str, str],
    current_names: set[str],
) -> list[DisappearanceFinding]:
    """Names in `prev` (mapping name → last_seen_at) but not in `current_names`.

    Deterministic ordering — sorted alphabetically so consecutive
    auditor runs produce the same output.
    """
    missing = sorted(set(prev) - current_names)
    return [
        DisappearanceFinding(name=n, last_seen_at=prev[n])
        for n in missing
    ]


def load_snapshot(path: Path) -> dict[str, str]:
    """Load the previous snapshot. Missing / corrupt files → empty dict."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, str):
            out[k] = v
    return out


def save_snapshot(path: Path, names: set[str], now_iso: str) -> None:
    """Atomically write the current snapshot.

    Each name maps to `now_iso` so the next scan can report when it
    was last seen. Atomic write avoids leaving a half-written file
    if the process dies mid-write.
    """
    payload = {n: now_iso for n in sorted(names)}
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(path)
    except OSError as e:
        log.warning("disappearance: couldn't write snapshot: %s", e)


__all__ = [
    "DisappearanceFinding",
    "diff_snapshots",
    "load_snapshot",
    "save_snapshot",
]
