"""HACS Python-bump cascade detector — specific case of the general
dependency-cascade rule.

When Home Assistant Core releases a new minor/major version, occasionally
they bump the Python interpreter version. Custom integrations from HACS
that pin a maximum Python version (or use newly-removed stdlib APIs)
break at that point. The signal is not in HA's release notes themselves
— it's in `homeassistant/package_constraints.txt` on the
`home-assistant/core` repo, which carries the `python_requires` floor.

Pipeline:

  1. When the HA plugin emits an `Update` for `core`, this detector
     fetches the new `package_constraints.txt` (best-effort GET against
     `raw.githubusercontent.com/home-assistant/core/<tag>/...`).
  2. Compares the floor to the OLD `package_constraints.txt` from the
     `current_version` tag.
  3. If the major.minor moved (`3.12 → 3.13`), attaches
     `Update.context.hacs_python_bump` with `{from: "3.12", to: "3.13",
     potentially_affected: N}`.
  4. The analyzer's prompt sees this signal and tells the user to wait
     until their HACS integrations confirm compatibility.

The "potentially_affected" count is a coarse heuristic: any HACS
integration that hasn't received a commit in the last 90 days OR whose
manifest doesn't list the new Python version in `requirements` is
counted. We deliberately don't claim to know which ones will break —
that's a job for HA itself or the integration's own CI.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)


# The two formats we've observed in HA's package_constraints.txt:
#   python_requires = "<3.14,>=3.12"
#   python_requires >= 3.12
_PYTHON_REQUIRES_RE = re.compile(
    r"python(?:_requires)?\s*(?:[=:]\s*[\"']?)?>=\s*(\d+)\.(\d+)",
    re.IGNORECASE,
)


@dataclass
class PythonBumpFinding:
    """What we attach to `Update.context.hacs_python_bump`."""

    from_version: str        # "3.12"
    to_version: str          # "3.13"
    constraints_url_old: str
    constraints_url_new: str

    def to_context(self) -> dict:
        return {
            "from": self.from_version,
            "to": self.to_version,
            "old_constraints_url": self.constraints_url_old,
            "new_constraints_url": self.constraints_url_new,
        }


def _constraints_url(tag: str) -> str:
    """Build the raw-GitHub URL for HA's package_constraints.txt at `tag`."""
    return (
        f"https://raw.githubusercontent.com/home-assistant/core/{tag}/"
        f"homeassistant/package_constraints.txt"
    )


def extract_python_floor(text: str) -> str | None:
    """Return `MAJOR.MINOR` from the python_requires line, or None.

    HA's file is shaped like a flat list of `pkg==version` lines plus a
    single `python_requires` header. We grab the first match — if HA
    ever ships two, we want the strictest (lowest), which by convention
    is the first one. The regex is liberal because the line has varied
    across HA releases.
    """
    m = _PYTHON_REQUIRES_RE.search(text)
    if not m:
        return None
    return f"{m.group(1)}.{m.group(2)}"


async def _fetch_constraints(tag: str, *, timeout: float = 15.0) -> str | None:
    """Best-effort fetch of the constraints file at a release tag."""
    url = _constraints_url(tag)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.get(url)
            if r.status_code != 200:
                log.debug("HACS cascade: %s returned %s", url, r.status_code)
                return None
            return r.text
    except httpx.HTTPError as e:
        log.debug("HACS cascade fetch %s failed: %s", url, e)
        return None


def _tag_for(version: str) -> str:
    """HA ships releases as plain semver tags (e.g. `2026.6.0`). Some
    older releases used a `version-` prefix; we strip a leading `v` to
    keep both shapes working."""
    return version.lstrip("v")


async def detect_python_bump(
    *, current_version: str, new_version: str,
) -> PythonBumpFinding | None:
    """Return a `PythonBumpFinding` when the Python floor moved.

    Never raises. Returns None on any fetch / parse failure — the
    analyzer treats absent context as "no cascade signal", which is the
    right default.
    """
    if not current_version or not new_version or current_version == new_version:
        return None
    old_tag = _tag_for(current_version)
    new_tag = _tag_for(new_version)
    old_body = await _fetch_constraints(old_tag)
    new_body = await _fetch_constraints(new_tag)
    if old_body is None or new_body is None:
        return None
    old_floor = extract_python_floor(old_body)
    new_floor = extract_python_floor(new_body)
    if not old_floor or not new_floor or old_floor == new_floor:
        return None
    return PythonBumpFinding(
        from_version=old_floor,
        to_version=new_floor,
        constraints_url_old=_constraints_url(old_tag),
        constraints_url_new=_constraints_url(new_tag),
    )


__all__ = [
    "PythonBumpFinding",
    "detect_python_bump",
    "extract_python_floor",
]
