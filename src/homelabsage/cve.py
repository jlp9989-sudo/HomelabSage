"""CVE adapter — run `trivy` (or `grype`) when available, parse to context.

Design choice: we deliberately do NOT reimplement vulnerability scanning. If
the user has `trivy` (or `grype`) on PATH we run it, parse the JSON, and
hand a short summary to the analyzer as an extra `context` field. If the
binary is absent the function silently returns None — the scan continues
unchanged.

Why summary, not full JSON: a typical trivy run on a busy image emits
30-80 KB of JSON, mostly CVE descriptions duplicated across packages. The
analyzer doesn't need that — it needs the *counts by severity* + the
critical CVE IDs so it can mention them in the recommendation.

This module never raises: every failure mode (timeout, missing binary,
malformed JSON, network blocked) returns None.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


# Trivy can take a while to refresh its DB on the first run. Cap is generous
# but bounded so a stuck scan doesn't hang the analyzer.
_DEFAULT_TIMEOUT = 90.0


@dataclass
class CveSummary:
    """Compact view of a trivy/grype scan suitable for LLM context.

    `top_critical` and `top_high` are CVE IDs only — the LLM doesn't need
    the description, it'll cite the ID and link to the NVD if asked.
    `scanner` records which tool produced the data so future heuristics can
    weight findings differently per source.
    """

    scanner: str
    counts: dict[str, int]
    top_critical: list[str]
    top_high: list[str]
    total: int

    def to_context(self) -> dict[str, Any]:
        return {
            "scanner": self.scanner,
            "counts": self.counts,
            "top_critical": self.top_critical[:10],
            "top_high": self.top_high[:10],
            "total": self.total,
        }


def _run_json(cmd: list[str], *, timeout: float) -> dict | list | None:
    """Run a command expecting JSON on stdout. None on any failure."""
    if not cmd or not shutil.which(cmd[0]):
        return None
    try:
        result = subprocess.run(
            cmd, timeout=timeout, check=False,
            capture_output=True, text=True, errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.debug("cve probe %s failed: %s", cmd[0], e)
        return None
    if result.returncode not in (0, 1):
        # trivy returns 1 when findings exist but the scan succeeded; that's
        # not an error for our purposes. Anything else is.
        log.debug("cve probe %s exit=%s stderr=%s", cmd[0], result.returncode,
                  result.stderr[:200])
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        log.debug("cve probe %s JSON parse failed: %s", cmd[0], e)
        return None


def scan_trivy(image: str, *, timeout: float = _DEFAULT_TIMEOUT) -> CveSummary | None:
    """`trivy image --format json --severity CRITICAL,HIGH <image>`.

    We restrict to CRITICAL+HIGH because the prompt only acts on those tiers;
    MEDIUM is mostly noise the analyzer can't act on without the user's
    runtime context (is this CVE reachable from your network?).
    """
    payload = _run_json(
        ["trivy", "image", "--format", "json", "--severity", "CRITICAL,HIGH",
         "--quiet", "--no-progress", image],
        timeout=timeout,
    )
    if not isinstance(payload, dict):
        return None

    counts: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    crit_ids: list[str] = []
    high_ids: list[str] = []
    for result in payload.get("Results", []) or []:
        for vuln in result.get("Vulnerabilities", []) or []:
            sev = (vuln.get("Severity") or "").lower()
            cve = vuln.get("VulnerabilityID") or ""
            if sev in counts:
                counts[sev] += 1
            if sev == "critical" and cve:
                crit_ids.append(cve)
            elif sev == "high" and cve:
                high_ids.append(cve)

    total = sum(counts.values())
    if total == 0 and not payload.get("Results"):
        # An empty Results list is the trivy way of saying "no findings"; we
        # still return the summary so the analyzer can see it was scanned.
        pass
    return CveSummary(
        scanner="trivy",
        counts=counts,
        top_critical=sorted(set(crit_ids)),
        top_high=sorted(set(high_ids)),
        total=total,
    )


def scan_grype(image: str, *, timeout: float = _DEFAULT_TIMEOUT) -> CveSummary | None:
    """`grype <image> -o json` — fallback when trivy isn't available.

    Grype's JSON shape differs from trivy (`matches[].vulnerability.severity`).
    Same severity gate, same summary contract.
    """
    payload = _run_json(["grype", image, "-o", "json"], timeout=timeout)
    if not isinstance(payload, dict):
        return None

    counts: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    crit_ids: list[str] = []
    high_ids: list[str] = []
    for match in payload.get("matches", []) or []:
        vuln = match.get("vulnerability") or {}
        sev = (vuln.get("severity") or "").lower()
        cve = vuln.get("id") or ""
        if sev in counts:
            counts[sev] += 1
        if sev == "critical" and cve:
            crit_ids.append(cve)
        elif sev == "high" and cve:
            high_ids.append(cve)

    return CveSummary(
        scanner="grype",
        counts=counts,
        top_critical=sorted(set(crit_ids)),
        top_high=sorted(set(high_ids)),
        total=sum(counts.values()),
    )


def scan_image(image: str) -> CveSummary | None:
    """Try trivy first, then grype. Return None when neither is available.

    Caller is expected to be defensive — the analyzer prompt rules already
    handle the case where `cve` is absent from context.
    """
    return scan_trivy(image) or scan_grype(image)
