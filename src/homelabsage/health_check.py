"""Post-update health check — verifies a container stays healthy after
the user marks an update APPLIED.

Why this exists separately from the analyzer: the analyzer reasons over
release notes BEFORE the upgrade. This module checks reality AFTER, and
catches the regressions release notes don't mention — the most common
being a silent ROCm/CUDA fallback to CPU when a GPU runtime changed
hands. Those incidents are nearly invisible (the container "runs", just
slowly) until the user notices a workload that used to be fast now
isn't.

Pipeline:
  1. When `set_status('applied')` fires for an update, the engine calls
     `queue_for_update(...)` which inserts into `health_check_queue`
     with `fire_at = now + grace_minutes`.
  2. The scheduler runs `run_due_checks()` periodically (or once per
     scan); any row with `fire_at <= now` is dequeued and probed.
  3. Each probe pulls `docker logs --since=<applied_at>` and walks the
     output for a small list of bad-signal regexes. The first match
     wins; the result is written to `health_checks`.
  4. The auditor surfaces failures on the next render.

Probe is best-effort; missing container = "container_gone" failure
signal (the user destroyed it before the probe, or upgrade failed
hard); unreachable daemon = the entire probe pass is skipped (logged).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from ._time import parse_iso, utcnow
from .config import HealthCheckConfig
from .db import Database

log = logging.getLogger(__name__)


# Built-in bad-signal patterns. Each one is a (signal_name, regex) tuple
# so the auditor row carries WHICH signal fired, not just "unhealthy".
_BUILTIN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("cuda_fallback", re.compile(
        r"(falling back to CPU|cuda.{0,20}fail|cuda.{0,20}not available|"
        r"no CUDA-capable device)", re.IGNORECASE,
    )),
    ("rocm_fallback", re.compile(
        r"(rocm.{0,20}fail|hip.{0,20}fail|HSA_OVERRIDE.*not.*found|"
        r"falling back from rocm)", re.IGNORECASE,
    )),
    ("oom", re.compile(
        r"(out of memory|oom-killed|killed.*memory|OOMKilled|"
        r"java.lang.OutOfMemoryError)", re.IGNORECASE,
    )),
    ("panic", re.compile(
        r"(runtime error: invalid memory|panic:|SIGSEGV|SIGABRT|"
        r"core dumped)",
    )),
    ("permission_denied", re.compile(
        r"(permission denied|EACCES|operation not permitted)",
        re.IGNORECASE,
    )),
]


@dataclass
class ProbeResult:
    """Single probe outcome — what gets written to `health_checks`."""

    update_id: str
    container_name: str
    ok: bool
    signal: str | None = None
    excerpt: str | None = None


def _container_running(container_name: str, *, socket: str) -> tuple[bool, str | None]:
    """(running, log_excerpt) — second tuple element is set when the
    container is gone and we can't even pull logs."""
    try:
        import docker as docker_sdk
    except Exception as e:
        log.warning("docker SDK import failed: %s", e)
        return False, "docker SDK not importable"
    try:
        client = docker_sdk.DockerClient(base_url=f"unix://{socket.lstrip('/')}")
        c = client.containers.get(container_name)
        state = (c.attrs.get("State", {}) or {})
        return bool(state.get("Running")), None
    except Exception as e:
        return False, f"container_gone: {e}"


def _fetch_logs_since(
    container_name: str, *, since: datetime, tail: int, socket: str,
) -> list[str]:
    try:
        import docker as docker_sdk
        client = docker_sdk.DockerClient(base_url=f"unix://{socket.lstrip('/')}")
        c = client.containers.get(container_name)
        raw = c.logs(
            since=since, timestamps=True, stdout=True, stderr=True, tail=tail,
        )
    except Exception as e:
        log.warning("health_check: logs fetch failed for %s: %s", container_name, e)
        return []
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace").splitlines()
    return [chunk.decode("utf-8", errors="replace") for chunk in raw]


def scan_lines(
    lines: list[str], *, extra_patterns: list[str] | None = None,
) -> tuple[str, str] | None:
    """First-match scan over the log slice.

    Returns `(signal_name, matching_line)` or None when nothing matched.
    Extra patterns are compiled lazily; bad regexes are skipped with a
    warning so a single typo can't break the scan.
    """
    patterns = list(_BUILTIN_PATTERNS)
    for raw in extra_patterns or []:
        try:
            patterns.append((f"custom:{raw[:24]}", re.compile(raw, re.IGNORECASE)))
        except re.error:
            log.warning("health_check: invalid extra pattern %r — ignored", raw)
    for line in lines:
        for name, pat in patterns:
            if pat.search(line):
                return name, line.strip()[:500]
    return None


def queue_for_update(
    db: Database,
    *,
    update_id: str,
    container_name: str,
    cfg: HealthCheckConfig,
    now: datetime | None = None,
) -> None:
    """Insert a probe N minutes out. Called by the engine on
    `status → APPLIED`. No-op when the feature is disabled."""
    if not cfg.enabled:
        return
    fire_at = (now or utcnow()) + timedelta(minutes=cfg.grace_minutes)
    db.queue_health_check(
        update_id=update_id, container_name=container_name, fire_at=fire_at,
    )


def probe_one(
    *,
    update_id: str,
    container_name: str,
    queued_at: datetime,
    cfg: HealthCheckConfig,
    socket: str,
) -> ProbeResult:
    """Run a single probe — split out so tests can drive it directly."""
    running, gone_reason = _container_running(container_name, socket=socket)
    if gone_reason:
        return ProbeResult(
            update_id=update_id, container_name=container_name,
            ok=False, signal="container_gone", excerpt=gone_reason,
        )
    if not running:
        return ProbeResult(
            update_id=update_id, container_name=container_name,
            ok=False, signal="not_running", excerpt="container exists but is not running",
        )
    lines = _fetch_logs_since(
        container_name, since=queued_at,
        tail=cfg.log_tail_lines, socket=socket,
    )
    match = scan_lines(lines, extra_patterns=cfg.extra_patterns)
    if match is None:
        return ProbeResult(
            update_id=update_id, container_name=container_name, ok=True,
        )
    return ProbeResult(
        update_id=update_id, container_name=container_name,
        ok=False, signal=match[0], excerpt=match[1],
    )


async def run_due_checks(
    cfg: HealthCheckConfig,
    db: Database,
    *,
    socket: str = "/var/run/docker.sock",
    now: datetime | None = None,
) -> list[ProbeResult]:
    """Process every queued probe whose `fire_at` has passed.

    Each one is awaited via `asyncio.to_thread` because both the docker
    SDK lookups + log fetch are blocking. Results are persisted and
    returned for the caller to log / count.
    """
    if not cfg.enabled:
        return []
    rows = db.due_health_checks(now=now)
    results: list[ProbeResult] = []
    for row in rows:
        # parse_iso raises on malformed strings by design; we want the
        # probe to keep going on bad rows (fall back to "right now" so
        # the log scan still produces something usable).
        try:
            queued_at = parse_iso(row.get("queued_at", "")) or utcnow()
        except ValueError:
            log.warning(
                "health_check: malformed queued_at on row %s, using now()",
                row.get("update_id"),
            )
            queued_at = utcnow()
        update_id = str(row["update_id"])
        container_name = str(row["container_name"])
        result = await asyncio.to_thread(
            probe_one,
            update_id=update_id, container_name=container_name,
            queued_at=queued_at, cfg=cfg, socket=socket,
        )
        db.record_health_check(
            update_id=result.update_id,
            container_name=result.container_name,
            ok=result.ok, signal=result.signal, excerpt=result.excerpt,
        )
        db.delete_health_check_queue(update_id=result.update_id)
        results.append(result)
    return results
