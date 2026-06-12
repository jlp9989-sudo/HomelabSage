"""Will-it-fit detector — cross disk free space with the new image's size.

HomelabSage already knows two things separately:
  - how big the candidate image is (`image_size_growth` → `new_mib`), and
  - how much free space the user's storage has (`disk_pressure.paths`).

Neither alone answers the question a user actually asks before clicking
"apply": *will this pull even fit, or will it half-download and brick the
image cache?* Docker fails in ugly ways when `docker pull` runs the
filesystem out of space — truncated layers, a corrupt image GC, a
container that won't start. This module joins the two existing signals so
the analyzer can say "this update needs ~600 MiB and you have 410 MiB
free — free space first" BEFORE the user finds out the hard way.

Model — what a pull actually costs:
  A `docker pull` downloads + extracts the new image's layers while the
  OLD image stays on disk (it isn't removed until you recreate the
  container and prune). Overlay layer sharing means the real cost is
  often less, but we deliberately assume NO sharing — the conservative
  bound is the safe one to warn on. So the space the pull needs is, worst
  case, the full new-image size.

Verdict ladder (against `free_bytes`):
  - free < new_image                  → `wont_fit`  (critical)
  - free < new_image * headroom_ratio → `tight`     (high)
  - otherwise                         → fits, return None (no noise)

Pure function. No subprocess, no HTTP — the caller passes both numbers in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_MIB = 1024 * 1024

# How much slack beyond the raw image size we want left over after the pull
# before we stop warning. 1.5 = "the pull should leave at least half an
# image's worth of headroom"; below that a busy system writing logs/db
# during the pull can still tip over.
DEFAULT_HEADROOM_RATIO = 1.5


@dataclass(frozen=True)
class FitVerdict:
    """Result of a will-it-fit probe, ready to drop into `Update.context`.

    Sizes are carried in MiB (already rounded) because that's what the
    prompt rule quotes; `path` names the filesystem the verdict is about
    so the user knows which pool to clear.
    """

    verdict: str            # wont_fit | tight
    severity: str           # high | critical
    new_image_mib: float
    free_mib: float
    headroom_after_mib: float   # free - new_image; negative when wont_fit
    path: str

    def to_context(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "severity": self.severity,
            "new_image_mib": round(self.new_image_mib, 1),
            "free_mib": round(self.free_mib, 1),
            "headroom_after_mib": round(self.headroom_after_mib, 1),
            "path": self.path,
        }


def evaluate(
    *,
    new_image_bytes: int,
    free_bytes: int,
    path: str,
    headroom_ratio: float = DEFAULT_HEADROOM_RATIO,
) -> FitVerdict | None:
    """Return a verdict when the pull is at risk of not fitting, else None.

    `new_image_bytes` is the candidate image's total size (from the
    registry, via the `image_size_growth` probe). `free_bytes` is the free
    space on the filesystem that backs the docker image store, named by
    `path`. Both must be positive to produce a signal — a zero/unknown
    size or an unstat'able path returns None rather than a false alarm.

    `headroom_ratio < 1` is treated as 1.0 (the floor — you always need at
    least the image's own size); anything above adds slack.
    """
    if new_image_bytes <= 0 or free_bytes <= 0:
        return None
    ratio = max(1.0, headroom_ratio)
    headroom_after_bytes = free_bytes - new_image_bytes
    if free_bytes < new_image_bytes:
        verdict, severity = "wont_fit", "critical"
    elif free_bytes < new_image_bytes * ratio:
        verdict, severity = "tight", "high"
    else:
        return None
    return FitVerdict(
        verdict=verdict,
        severity=severity,
        new_image_mib=new_image_bytes / _MIB,
        free_mib=free_bytes / _MIB,
        headroom_after_mib=headroom_after_bytes / _MIB,
        path=path,
    )


__all__ = ["FitVerdict", "evaluate", "DEFAULT_HEADROOM_RATIO"]
