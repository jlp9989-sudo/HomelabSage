"""Backup-staleness adapter — probe restic / borg / kopia for snapshot freshness.

Why: HomelabSage already advises *what* to update, but the most user-hostile
incident in a homelab is "you upgraded confidently and your backups had been
silently broken for 6 weeks". This adapter asks each configured backup repo
"when did you last receive a snapshot?" and emits one finding per repo that
crosses the staleness thresholds.

Design choices:
  - The adapter runs the user's existing backup tool — we never reinvent
    snapshotting. Three tools are supported because they cover ~95 % of
    self-hosted setups (restic + borg + kopia). New ones are one
    `_probe_<tool>` function away.
  - Credentials never live in HomelabSage config. The user supplies `env`
    per-repo (e.g. `RESTIC_REPOSITORY`, `RESTIC_PASSWORD_FILE`, `BORG_REPO`,
    `BORG_PASSPHRASE`). HomelabSage simply passes them through to the
    subprocess. The same env file the user already wrote for their nightly
    cron works as-is.
  - Failure is the boring case: missing binary, unreachable repo, wrong
    password — each returns a `BackupHealth` with `ok=False` + the reason.
    Never raises.
  - No LLM in the loop. Staleness in days is a number, not a verdict — it
    gets compared to two thresholds and gets a severity.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ._time import parse_iso, utcnow

log = logging.getLogger(__name__)


# Capped to keep the scan responsive — restic can take a while to enumerate
# snapshots on a cold cache against an S3 backend, but we only ask for the
# JSON listing so 60 s is comfortable.
_DEFAULT_TIMEOUT = 60.0


@dataclass
class BackupHealth:
    """Per-repo probe result.

    `ok=False` means we could not get a freshness signal; `reason` is the
    short human-readable cause for the auditor cite line. When `ok=True`,
    `latest_snapshot_at` is always set; `snapshot_count` may be 0 in the
    edge case of a freshly-init'd repo (the analyzer treats that as a
    distinct signal — "your repo is empty" is not the same as "stale").
    """

    repo_name: str
    tool: str
    ok: bool
    reason: str
    latest_snapshot_at: datetime | None = None
    snapshot_count: int = 0
    staleness_days: int | None = None
    # `severity` is computed from staleness against the repo's own thresholds
    # so the auditor + analyzer don't have to rediscover the rule.
    severity: str = "info"  # info | medium | high | critical

    def to_context(self) -> dict:
        return {
            "repo": self.repo_name,
            "tool": self.tool,
            "ok": self.ok,
            "reason": self.reason,
            "latest_snapshot_at": (
                self.latest_snapshot_at.isoformat() if self.latest_snapshot_at else None
            ),
            "snapshot_count": self.snapshot_count,
            "staleness_days": self.staleness_days,
            "severity": self.severity,
        }


# ─── subprocess helper ─────────────────────────────────────────────────


def _run(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> tuple[int, str, str] | None:
    """Run a command, returning (returncode, stdout, stderr) or None on failure.

    Returns None when the binary is missing — the caller distinguishes
    "tool not installed" from "tool ran but errored".
    """
    if not cmd or not shutil.which(cmd[0]):
        return None
    # Merge env on top of os.environ so PATH + HOME stay intact; the user's
    # `env` keys take precedence.
    merged = dict(os.environ)
    if env:
        merged.update(env)
    try:
        result = subprocess.run(
            cmd, env=merged, timeout=timeout, check=False,
            capture_output=True, text=True, errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.debug("backup probe %s failed: %s", cmd[0], e)
        return None
    return result.returncode, result.stdout, result.stderr


# ─── per-tool probes ───────────────────────────────────────────────────


def probe_restic(
    *, env: dict[str, str] | None = None, binary: str = "restic",
    timeout: float = _DEFAULT_TIMEOUT,
) -> tuple[datetime | None, int, str]:
    """Run `restic snapshots --json` and return (latest_when, count, reason).

    `latest_when=None` with a populated reason means the probe failed; the
    reason is short enough to appear inline in the audit report.
    """
    out = _run([binary, "snapshots", "--json"], env=env, timeout=timeout)
    if out is None:
        return None, 0, "restic binary not on PATH"
    rc, stdout, stderr = out
    if rc != 0:
        # restic exits non-zero on bad password / unreachable repo / locked
        # repo — surface the first stderr line as the reason.
        err = (stderr.strip().splitlines() or ["restic returned non-zero"])[0]
        return None, 0, f"restic failed: {err[:200]}"
    try:
        snapshots = json.loads(stdout)
    except json.JSONDecodeError:
        return None, 0, "restic JSON parse failed"
    if not isinstance(snapshots, list):
        return None, 0, "unexpected restic JSON shape"
    if not snapshots:
        return None, 0, "restic repo has no snapshots yet"
    times: list[datetime] = []
    for snap in snapshots:
        when_raw = snap.get("time")
        if not when_raw:
            continue
        try:
            when = parse_iso(when_raw)
        except ValueError:
            continue  # malformed timestamp — skip this snapshot
        if when:
            times.append(when)
    if not times:
        return None, len(snapshots), "snapshots present but no parseable times"
    latest = max(times)
    return latest, len(snapshots), "restic ok"


def probe_borg(
    *, env: dict[str, str] | None = None, binary: str = "borg",
    timeout: float = _DEFAULT_TIMEOUT,
) -> tuple[datetime | None, int, str]:
    """`borg list --json` against the repo named by `BORG_REPO`."""
    out = _run([binary, "list", "--json"], env=env, timeout=timeout)
    if out is None:
        return None, 0, "borg binary not on PATH"
    rc, stdout, stderr = out
    if rc != 0:
        err = (stderr.strip().splitlines() or ["borg returned non-zero"])[0]
        return None, 0, f"borg failed: {err[:200]}"
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None, 0, "borg JSON parse failed"
    archives = (payload or {}).get("archives") or []
    if not archives:
        return None, 0, "borg repo has no archives yet"
    times: list[datetime] = []
    for arc in archives:
        # borg uses `time` (ISO without tz) — parse_iso upgrades to UTC.
        when_raw = arc.get("time") or arc.get("start")
        if not when_raw:
            continue
        try:
            when = parse_iso(when_raw)
        except ValueError:
            continue  # malformed timestamp — skip this snapshot
        if when:
            times.append(when)
    if not times:
        return None, len(archives), "archives present but no parseable times"
    return max(times), len(archives), "borg ok"


def probe_kopia(
    *, env: dict[str, str] | None = None, binary: str = "kopia",
    timeout: float = _DEFAULT_TIMEOUT,
) -> tuple[datetime | None, int, str]:
    """`kopia snapshot list --all --json` — assumes `kopia repository connect`
    has been done out-of-band (the standard kopia UX)."""
    out = _run(
        [binary, "snapshot", "list", "--all", "--json"], env=env, timeout=timeout,
    )
    if out is None:
        return None, 0, "kopia binary not on PATH"
    rc, stdout, stderr = out
    if rc != 0:
        err = (stderr.strip().splitlines() or ["kopia returned non-zero"])[0]
        return None, 0, f"kopia failed: {err[:200]}"
    try:
        snapshots = json.loads(stdout)
    except json.JSONDecodeError:
        return None, 0, "kopia JSON parse failed"
    if not isinstance(snapshots, list):
        return None, 0, "unexpected kopia JSON shape"
    if not snapshots:
        return None, 0, "kopia has no snapshots yet"
    times: list[datetime] = []
    for snap in snapshots:
        when_raw = snap.get("endTime") or snap.get("startTime")
        if not when_raw:
            continue
        try:
            when = parse_iso(when_raw)
        except ValueError:
            continue  # malformed timestamp — skip this snapshot
        if when:
            times.append(when)
    if not times:
        return None, len(snapshots), "snapshots present but no parseable times"
    return max(times), len(snapshots), "kopia ok"


_PROBES = {
    "restic": probe_restic,
    "borg": probe_borg,
    "kopia": probe_kopia,
}


# ─── orchestrator ──────────────────────────────────────────────────────


def _severity_from_staleness(
    staleness_days: int | None, *, warn_after_days: int, critical_after_days: int,
) -> str:
    """Map staleness days to a severity bucket.

    `None` (probe failed) is treated as `high` — the user wanted backups and
    we can't tell whether they're working. Better noisy than silent.
    """
    if staleness_days is None:
        return "high"
    if staleness_days >= critical_after_days:
        return "critical"
    if staleness_days >= warn_after_days:
        return "medium"
    return "info"


def probe_repo(
    *,
    name: str,
    tool: str,
    env: dict[str, str] | None = None,
    binary: str | None = None,
    warn_after_days: int = 2,
    critical_after_days: int = 7,
    timeout: float = _DEFAULT_TIMEOUT,
    now: datetime | None = None,
) -> BackupHealth:
    """Probe one repo and produce a `BackupHealth`.

    Unknown `tool` is treated as a failure (not raised) so a typo in YAML
    doesn't break the whole audit run.
    """
    probe = _PROBES.get(tool.lower())
    if probe is None:
        return BackupHealth(
            repo_name=name, tool=tool, ok=False,
            reason=f"unsupported tool {tool!r} — pick one of: restic, borg, kopia",
            severity="high",
        )
    kwargs: dict = {"env": env, "timeout": timeout}
    if binary:
        kwargs["binary"] = binary
    latest, count, reason = probe(**kwargs)
    if latest is None:
        return BackupHealth(
            repo_name=name, tool=tool, ok=False, reason=reason,
            snapshot_count=count, severity="high",
        )
    end = now or utcnow()
    if end.tzinfo is None:
        from datetime import UTC
        end = end.replace(tzinfo=UTC)
    if latest.tzinfo is None:
        from datetime import UTC
        latest = latest.replace(tzinfo=UTC)
    delta: timedelta = end - latest
    staleness_days = max(0, int(delta.total_seconds() // 86400))
    return BackupHealth(
        repo_name=name, tool=tool, ok=True, reason=reason,
        latest_snapshot_at=latest, snapshot_count=count,
        staleness_days=staleness_days,
        severity=_severity_from_staleness(
            staleness_days,
            warn_after_days=warn_after_days,
            critical_after_days=critical_after_days,
        ),
    )


@dataclass
class BackupHealthReport:
    """Snapshot of every configured repo. Consumed by the auditor."""

    generated_at: datetime = field(default_factory=utcnow)
    results: list[BackupHealth] = field(default_factory=list)

    def healthy(self) -> bool:
        """True iff every probe ran AND no repo is in `high`/`critical`."""
        return all(r.ok and r.severity in ("info", "medium") for r in self.results)
