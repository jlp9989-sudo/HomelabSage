"""Detect containers that were OOM-killed since the last scan.

`docker inspect` exposes `State.OOMKilled: bool` — the daemon flips
this when the kernel reaped the container's main process for being
over its memory limit. The flag survives a restart, so a container
that OOM'd last night is visible until the next OOM (or until the
user stops + recreates).

Pure function over `container.attrs["State"]`. The docker plugin
attaches the verdict to `Update.context["oom_killed"]`; the audit
extractor surfaces it as a `high`-severity finding.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OOMFinding:
    """Per-container OOM-killed verdict."""

    oom_killed: bool
    exit_code: int
    finished_at: str | None    # ISO 8601 when known
    severity: str              # always "high" when oom_killed=True

    def to_context(self) -> dict:
        return {
            "oom_killed": self.oom_killed,
            "exit_code": self.exit_code,
            "finished_at": self.finished_at,
            "severity": self.severity,
        }


def evaluate(state: dict) -> OOMFinding | None:
    """Return a finding only when the OOMKilled flag is set.

    Best-effort: malformed `State` returns None (treat as "no
    signal" — never raise on a partial inspect dict).
    """
    if not isinstance(state, dict):
        return None
    if not bool(state.get("OOMKilled")):
        return None
    exit_code = int(state.get("ExitCode") or 0)
    finished_raw = state.get("FinishedAt")
    finished: str | None = None
    if isinstance(finished_raw, str) and finished_raw:
        # docker emits "0001-01-01T00:00:00Z" for never-finished
        # containers; treat as missing.
        finished = (
            None if finished_raw.startswith("0001-") else finished_raw
        )
    return OOMFinding(
        oom_killed=True,
        exit_code=exit_code,
        finished_at=finished,
        severity="high",
    )


__all__ = ["OOMFinding", "evaluate"]
