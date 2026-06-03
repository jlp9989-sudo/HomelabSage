"""Image-pin enforcement — flag updates that would cross a user-declared pin.

Real-world pattern: the user has a service that legitimately must stay
on an older major version (Elasticsearch 8.x because RAGFlow can't read
9.x indices; PostgreSQL 15 because pg_repack on 16 has open issues;
homeassistant/core 2025.x because the user runs a HACS integration that
hasn't been ported). Today they manage these by:

  - hoping they remember the constraint when they see the update, or
  - writing a curator note that *says* "versionlocked at 8.x because X"
    — which the analyzer picks up SOMETIMES depending on noise.

That's not safe enough. This module makes the pin machine-readable:

  image_pins:
    enabled: true
    pins:
      elasticsearch: "8.*"
      postgres: "15.*"
      homeassistant/core: "<=2026.5"

When a detected update's `new_version` would cross the pin, the
analyzer sees `context.pin_violation = {pin: "8.*", new_version: "9.0"}`
and the prompt rule downgrades the recommendation to `hold` with a
specific reason. No more relying on prose to do safety-critical work.

Match shape:
  - Exact match: `pin == new_version`. Most restrictive.
  - Glob with `*`: `8.*` matches `8.5.2` but not `9.0.0`. Common case.
  - SemVer-comparison prefix `<=` / `<` / `>=` / `>` / `==`: parse the
    right side as a Version and compare; falls back to string compare
    on parse failure (which is safe — the rule fires conservatively).

Pin keys match against subject (the container name OR the GitHub repo
slug for watched repos). Match is case-insensitive on the key.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)


_OP_PREFIX_RE = re.compile(r"^\s*(<=|<|>=|>|==)\s*(.+)\s*$")


@dataclass
class PinVerdict:
    """What we attach to `Update.context.pin_violation`."""

    pin: str
    pin_subject: str
    new_version: str
    reason: str

    def to_context(self) -> dict:
        return {
            "pin": self.pin,
            "pin_subject": self.pin_subject,
            "new_version": self.new_version,
            "reason": self.reason,
        }


def _normalise(version: str) -> str:
    """Strip the common leading `v` so `v1.2.3` matches `1.2.3`."""
    return version.strip().lstrip("v")


def _matches_pin(new_version: str, pin: str) -> tuple[bool, str]:
    """Return (allowed, reason). `allowed=False` → the update CROSSES the pin.

    When the `packaging` library is unavailable OR can't parse one side
    of an operator pin, we return `allowed=True` with a warning reason.
    The alternative (blocking on parse failure) silently freezes
    legitimate upgrades; the user is better served by a no-op than a
    silent veto they don't understand.
    """
    new = _normalise(new_version)
    pin_stripped = _normalise(pin)
    # Operator prefix first — `<=2026.5`
    m = _OP_PREFIX_RE.match(pin_stripped)
    if m:
        op, right = m.group(1), m.group(2)
        try:
            from packaging.version import InvalidVersion, Version
        except ImportError:
            return True, f"packaging unavailable — could not enforce {op}{right}"
        try:
            left_v = Version(new)
            right_v = Version(right)
        except InvalidVersion:
            return True, (
                f"could not parse one of {new!r}/{right!r} as a Version — "
                f"skipping operator pin {op}{right}"
            )
        cmp = {
            "<": left_v < right_v,
            "<=": left_v <= right_v,
            ">": left_v > right_v,
            ">=": left_v >= right_v,
            "==": left_v == right_v,
        }[op]
        if cmp:
            return True, f"satisfies {op}{right}"
        return False, f"crosses pin {op}{right}"
    # Glob with `*`
    if "*" in pin_stripped:
        if fnmatch.fnmatch(new, pin_stripped):
            return True, f"matches glob {pin_stripped}"
        return False, f"does not match glob {pin_stripped}"
    # Exact match
    if new == pin_stripped:
        return True, f"matches exact pin {pin_stripped}"
    return False, f"differs from exact pin {pin_stripped}"


def evaluate(
    *,
    subject: str,
    new_version: str,
    pins: dict[str, str],
) -> PinVerdict | None:
    """Return a verdict when the update would cross a pin, else None.

    `pins` maps `subject_pattern` → `pin_spec`. Subjects are
    case-insensitive. Exact-match keys take precedence over glob keys,
    so a YAML order like `{"*": ..., "mealie": ...}` still hits the
    explicit `mealie` entry. Within the same precedence tier the user's
    insertion order wins (YAML preserves it).
    """
    subj_lower = subject.lower()
    # Sort: exact-match keys first (no `*`), globs after. Stable across
    # equally-specific entries (Python's sort is stable).
    sorted_keys = sorted(pins.keys(), key=lambda k: ("*" in k, list(pins).index(k)))
    for key in sorted_keys:
        spec = pins[key]
        key_lower = key.lower()
        if "*" in key_lower:
            if not fnmatch.fnmatchcase(subj_lower, key_lower):
                continue
        elif subj_lower != key_lower:
            continue
        allowed, reason = _matches_pin(new_version, spec)
        if allowed:
            return None
        return PinVerdict(
            pin=spec, pin_subject=key, new_version=new_version, reason=reason,
        )
    return None


__all__ = ["PinVerdict", "evaluate"]
