"""Proactive log-anomaly detector — flags containers whose recent error
rate spiked compared to their own rolling baseline.

Distinct from `csi` (which is post-mortem, on demand) and from `log_samples`
(the DB storage). This module is the orchestrator that:

  1. Walks every running container,
  2. Pulls the last `lookback_minutes` of docker logs,
  3. Counts ERROR / WARN lines using the same heuristic the CSI uses,
  4. Records the sample in `log_samples`,
  5. Compares the latest rate against the per-container baseline,
  6. Writes a `log_anomalies` row when the rate is `sigma_threshold` σ
     above the baseline AND the baseline has at least `min_samples`.

Step 6 only fires for containers that have been observed for `min_samples`
scans — newly added containers spend a warm-up period accumulating
baseline data without ever firing a false positive. That warm-up is the
single piece of subtlety in the whole module; everything else is
deterministic counting.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import timedelta

from ._time import utcnow
from .config import LogAnomalyConfig
from .csi import fetch_docker_logs
from .db import Database

log = logging.getLogger(__name__)


# Separate from CSI's regex — here we *only* count ERROR/FATAL/PANIC, not
# WARN, because warnings are routine on most containers and the baseline
# absorbs them into noise. Anomaly detection cares about hard-failure rate.
#
# I6: previously this regex included `err` (3-char) and `crit` (4-char)
# alternatives. Word-boundary `\b` doesn't help when the bare word is the
# substring inside `application_error_handler` (no, that's safe), but it
# DID match `derr`, `Werror=`, `nginx-stderr` (`-` is non-word so `stderr`
# matches), and `criterion`, `critical_section` (`crit` followed by `_`).
# Pinning to the full English forms removes the false-positives without
# losing any real signal — every logger that emits truncated `err` also
# emits `error` or `fatal` somewhere.
_ERROR_RE = re.compile(
    r"\b(error|fatal|panic|critical|traceback|exception)\b",
    re.IGNORECASE,
)
_WARN_RE = re.compile(r"\b(warn(ing)?)\b", re.IGNORECASE)
_NOISE_LINE_RE = re.compile(r"^[\s\d:.\-T]+$")


@dataclass
class LogScanSample:
    """One container's per-scan tally."""

    container_name: str
    error_count: int
    warn_count: int
    window_minutes: int


@dataclass
class AnomalySignal:
    """Spike emitted when a sample crossed the σ threshold."""

    container_name: str
    error_rate: float       # errors per minute
    baseline_mean: float
    baseline_std: float
    sigma: float


def count_errors_warns(lines: list[str]) -> tuple[int, int]:
    """Walk a log slice, count ERROR vs WARN matches.

    Empty / pure-timestamp lines are skipped so a `journalctl` timestamp
    column never inflates the counts.
    """
    err = 0
    warn = 0
    for raw in lines:
        line = raw.rstrip("\n")
        if not line.strip() or _NOISE_LINE_RE.match(line):
            continue
        if _ERROR_RE.search(line):
            err += 1
            continue
        if _WARN_RE.search(line):
            warn += 1
    return err, warn


def _list_running_containers(socket: str) -> list[str]:
    """Best-effort enumeration. Empty list on any failure (daemon down, etc)."""
    try:
        import docker as docker_sdk
    except Exception as e:  # pragma: no cover — docker SDK is a hard dep
        log.warning("docker SDK import failed: %s", e)
        return []
    try:
        client = docker_sdk.DockerClient(base_url=f"unix://{socket.lstrip('/')}")
        containers = client.containers.list()
    except Exception as e:
        log.warning("log_anomaly: docker daemon unreachable: %s", e)
        return []
    return [c.name for c in containers]


def scan_container(
    container_name: str,
    *,
    lookback_minutes: int,
    socket: str = "/var/run/docker.sock",
    fetch_fn=None,
) -> LogScanSample:
    """Fetch + count for one container. `fetch_fn` injection lets tests
    pass in synthetic log lines without spinning up docker."""
    since = utcnow().replace(microsecond=0) - timedelta(minutes=lookback_minutes)
    if fetch_fn is not None:
        lines = fetch_fn(container_name, since=since)
    else:
        lines = fetch_docker_logs(container_name, since=since, socket=socket)
    err, warn = count_errors_warns(lines)
    return LogScanSample(
        container_name=container_name,
        error_count=err,
        warn_count=warn,
        window_minutes=lookback_minutes,
    )


def evaluate(
    db: Database,
    sample: LogScanSample,
    *,
    sigma_threshold: float,
    min_samples: int,
) -> AnomalySignal | None:
    """Compare a fresh sample against the baseline; return a signal or None.

    Side-effect free aside from the baseline read — the caller decides
    whether to persist the anomaly. Splitting persistence out lets the
    test suite assert the math without writing rows.
    """
    rate = (
        sample.error_count / sample.window_minutes
        if sample.window_minutes else 0.0
    )
    mean, std, n = db.baseline_for(container_name=sample.container_name)
    if n < min_samples:
        return None
    # Zero-variance baseline: treat any non-zero error as an anomaly only
    # when it's at least 1/min — protects against the "always-quiet
    # container" suddenly firing once and being labelled critical.
    if std == 0:
        if rate >= max(1.0, mean * 2):
            return AnomalySignal(
                container_name=sample.container_name,
                error_rate=rate, baseline_mean=mean, baseline_std=0.0,
                sigma=float(sigma_threshold),
            )
        return None
    sigma = (rate - mean) / std
    if sigma < sigma_threshold:
        return None
    return AnomalySignal(
        container_name=sample.container_name,
        error_rate=rate, baseline_mean=mean, baseline_std=std, sigma=sigma,
    )


def _compile_skip(skip_patterns: list[str]) -> list[re.Pattern[str]]:
    """Pre-compile the skip regexes once per scan instead of per container."""
    out: list[re.Pattern[str]] = []
    for pat in skip_patterns:
        try:
            out.append(re.compile(pat))
        except re.error:
            log.warning("log_anomaly: invalid skip regex %r — ignored", pat)
    return out


def _skip_container(name: str, compiled_patterns: list[re.Pattern[str]]) -> bool:
    return any(p.search(name) for p in compiled_patterns)


async def run_log_anomaly_scan(
    cfg: LogAnomalyConfig,
    db: Database,
    *,
    socket: str = "/var/run/docker.sock",
    container_names: list[str] | None = None,
) -> list[AnomalySignal]:
    """Walk every running container, sample, evaluate, persist.

    `container_names` injection lets the engine reuse the docker client
    listing it already did this scan (saves one round-trip). Tests use
    it to inject deterministic targets.
    """
    if not cfg.enabled:
        return []
    if container_names is None:
        # Push the docker enumeration to a worker thread — the SDK call is
        # blocking and we may be running inside the engine's event loop.
        container_names = await asyncio.to_thread(_list_running_containers, socket)
    signals: list[AnomalySignal] = []
    compiled_skip = _compile_skip(cfg.container_skip)
    for name in container_names:
        if _skip_container(name, compiled_skip):
            continue
        sample = await asyncio.to_thread(
            scan_container, name,
            lookback_minutes=cfg.lookback_minutes, socket=socket,
        )
        db.record_log_sample(
            container_name=sample.container_name,
            error_count=sample.error_count,
            warn_count=sample.warn_count,
            window_minutes=sample.window_minutes,
        )
        signal = evaluate(
            db, sample,
            sigma_threshold=cfg.sigma_threshold,
            min_samples=cfg.min_samples,
        )
        if signal is None:
            continue
        db.record_log_anomaly(
            container_name=signal.container_name,
            error_rate=signal.error_rate,
            baseline_mean=signal.baseline_mean,
            baseline_std=signal.baseline_std,
            sigma=signal.sigma,
        )
        signals.append(signal)
    return signals
