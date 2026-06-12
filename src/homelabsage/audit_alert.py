"""Fire an audit-diff webhook when new findings appear.

This is a separate channel from per-update push outputs. The
audit walks every signal already in the DB + live probes and
surfaces drift; a single rollup per scan is the right cadence
(per-finding pings would spam during noisy upgrade windows).

Payload shape (intentionally minimal; bring-your-own-receiver):

    {
      "type": "audit_diff",
      "generated_at": "2026-06-04T12:00:00+00:00",
      "new": [
        {"category": "cve", "severity": "high", "title": "...",
         "source_kind": "cve", "source_ref": "mealie@1.2.3", "cite": "..."}
      ],
      "resolved": [
        {"category": "abandoned", "source_kind": "repo_health",
         "source_ref": "<update-id>"}
      ],
      "counts_by_severity": {"high": 1, "medium": 2}
    }

Best-effort: per-URL failures log + continue; we don't block the
audit on a slow webhook.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .models import severity_order

log = logging.getLogger(__name__)



def _meets_floor(findings: list[dict[str, Any]], floor: str) -> bool:
    """True iff at least one new finding meets the severity floor."""
    floor_n = severity_order(floor, default=1)
    for f in findings:
        sev = str(f.get("severity") or "").lower()
        if severity_order(sev) >= floor_n:
            return True
    return False


def fire_if_new(
    *,
    webhook_urls: list[str],
    min_severity: str,
    timeout: float,
    diff: dict[str, Any],
    counts_by_severity: dict[str, int],
    generated_at: str,
) -> int:
    """Post the rollup to each webhook. Returns the success count.

    No-op when `webhook_urls` is empty or no NEW finding meets the
    severity floor. `resolved` items are reported but don't trigger
    the fire on their own — a finding clearing is not an alert.
    """
    new_findings = diff.get("new") or []
    if not webhook_urls or not new_findings:
        return 0
    if not _meets_floor(new_findings, min_severity):
        return 0

    payload = {
        "type": "audit_diff",
        "generated_at": generated_at,
        "new": new_findings,
        "resolved": diff.get("resolved") or [],
        "counts_by_severity": counts_by_severity,
    }
    success = 0
    with httpx.Client(timeout=timeout) as client:
        for url in webhook_urls:
            try:
                r = client.post(url, json=payload)
                r.raise_for_status()
                success += 1
            except httpx.HTTPError as e:
                log.warning("audit_alert: %s failed: %s", url, e)
    return success


__all__ = ["fire_if_new"]
