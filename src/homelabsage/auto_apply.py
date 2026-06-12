"""Auto-apply whitelist — opt-in safe-list of subjects allowed to flip
to APPLIED automatically when severity is at or below the configured
ceiling.

This is intentionally NARROW: most homelab updates deserve human eyes
on the breaking-changes list. But a handful of containers update many
times per month with effectively zero risk (a static frontend, a
linter image, a personal blog generator). For those, manual approval
is friction that produces no value.

Safety design:
  - Disabled by default.
  - Allow-list only — no regex matching, no "all containers" mode.
    A typo can't accidentally upgrade your reverse proxy.
  - Severity ceiling: items above `max_severity` are NEVER auto-applied
    even when the subject is allow-listed. Default `info`.
  - `breaking_changes` non-empty BLOCKS auto-apply regardless of
    severity — the breaking-change wall must be human-reviewed.
  - `pin_violation` BLOCKS auto-apply — the user-asserted pin always
    wins.
  - Auto-apply records a `notes_for_human` audit line in the DB so the
    user can see "X was auto-applied at YYYY-MM-DD HH:MM" later.

The check is pure (no DB writes) so the engine can evaluate it after
analysis, persist the decision, then flip the status in one place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .models import AnalyzedUpdate, Severity, severity_order

log = logging.getLogger(__name__)




@dataclass
class AutoApplyDecision:
    """Per-update verdict from `should_auto_apply`."""

    should_apply: bool
    reason: str         # human-readable reason for the audit trail


def should_auto_apply(
    item: AnalyzedUpdate,
    *,
    allowlist: list[str],
    max_severity: str = "info",
) -> AutoApplyDecision:
    """Return the auto-apply verdict.

    `allowlist` is an exact-match list of subjects (case-insensitive).
    No globs — the safety story rests on the user typing each entry.

    Order of checks matters: bail-out conditions are evaluated before
    the allow-list lookup so the most-restrictive rule wins.
    """
    subject_lower = (item.update.subject or "").lower()
    allow_lower = {s.lower() for s in allowlist or []}

    # Block 1: not in allow-list — short-circuit.
    if subject_lower not in allow_lower:
        return AutoApplyDecision(
            should_apply=False, reason="subject not in auto_apply.allowlist",
        )

    a = item.analysis
    # Block 2: no analysis → no verdict to act on.
    if a is None:
        return AutoApplyDecision(
            should_apply=False, reason="update is not analyzed yet",
        )

    # Block 3: severity above ceiling.
    ceiling = severity_order(max_severity or "info")
    sev_order = severity_order(a.severity)
    if sev_order > ceiling:
        return AutoApplyDecision(
            should_apply=False,
            reason=(
                f"severity {a.severity.value} exceeds auto_apply ceiling "
                f"{max_severity}"
            ),
        )

    # Block 4: breaking changes always block — the wall is human-only.
    if a.breaking_changes:
        return AutoApplyDecision(
            should_apply=False,
            reason=f"breaking_changes non-empty ({len(a.breaking_changes)} listed)",
        )

    # Block 5: pin violations always block.
    if (item.update.context or {}).get("pin_violation"):
        return AutoApplyDecision(
            should_apply=False, reason="image_pins violation",
        )

    return AutoApplyDecision(
        should_apply=True,
        reason=(
            f"subject in allowlist, severity={a.severity.value} ≤ {max_severity}, "
            f"no breaking_changes, no pin_violation"
        ),
    )


def severity_ceiling_value(name: str) -> Severity:
    """Convert the config string to a `Severity` enum for the prompt
    explainer. Defaults to INFO on unknown input — the safest reading."""
    try:
        return Severity(name)
    except ValueError:
        return Severity.INFO


__all__ = ["AutoApplyDecision", "should_auto_apply", "severity_ceiling_value"]
