"""Disk-pressure auditor.

Docker silently breaks in entertaining ways when the filesystem
that holds `/var/lib/docker` runs out of space — pulls fail half-
way, layer cache corruption, image GC bricks itself. A user who
just got "high severity update available" needs to know whether
applying it will fit on disk.

The module is a pure function over `shutil.disk_usage()` plus an
optional list of paths (cache pool, appdata share, docker root).
No 3rd-party dep, no subprocess. The caller passes the paths from
config; we never assume any default.

Severity buckets:
  - <5% free OR <5 GiB free  → critical
  - <10% free OR <20 GiB free → high
  - <20% free OR <50 GiB free → medium
  - otherwise → info (suppressed; we return None)
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass
class DiskFinding:
    """Per-path disk-pressure verdict."""

    path: str
    total_bytes: int
    free_bytes: int
    percent_free: float
    severity: str   # medium | high | critical

    def to_context(self) -> dict:
        return {
            "path": self.path,
            "total_bytes": self.total_bytes,
            "free_bytes": self.free_bytes,
            "percent_free": round(self.percent_free, 2),
            "severity": self.severity,
        }


_GIB = 1024 ** 3


def _severity(free_bytes: int, percent_free: float) -> str | None:
    """Two-axis severity: both % and absolute matter.

    A 4 TB pool with 50 GB free is "medium" by percent (>1%) but
    catastrophic by absolute (50 GB is one large image pull away
    from full). Conversely, a 500 GB SSD at 9% free has 45 GB free
    — same absolute, different timeline.

    We take the worse of the two.
    """
    if percent_free < 5.0 or free_bytes < 5 * _GIB:
        return "critical"
    if percent_free < 10.0 or free_bytes < 20 * _GIB:
        return "high"
    if percent_free < 20.0 or free_bytes < 50 * _GIB:
        return "medium"
    return None


def evaluate(paths: list[str]) -> list[DiskFinding]:
    """Probe each path; return only findings worth flagging.

    Paths that don't exist or can't be stat'd are skipped silently
    (the user may have aspirationally listed a pool that's not
    mounted in this container — we don't want to noise-bomb them).

    Duplicate filesystems (same st_dev) collapse to one report —
    `/mnt/user` and `/mnt/cache` may share a device on a single-pool
    Unraid; reporting both wastes the user's attention.
    """
    seen_devices: set[int] = set()
    out: list[DiskFinding] = []
    for raw in paths:
        p = Path(raw).expanduser()
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_dev in seen_devices:
            continue
        seen_devices.add(st.st_dev)
        try:
            usage = shutil.disk_usage(p)
        except OSError:
            continue
        total = usage.total or 0
        free = usage.free or 0
        pct = (free / total * 100.0) if total > 0 else 0.0
        sev = _severity(free, pct)
        if sev is None:
            continue
        out.append(DiskFinding(
            path=str(p),
            total_bytes=total,
            free_bytes=free,
            percent_free=pct,
            severity=sev,
        ))
    return out


__all__ = ["DiskFinding", "evaluate"]
