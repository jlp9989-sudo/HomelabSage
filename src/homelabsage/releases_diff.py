"""Multi-release diff summariser.

Every other Docker-update notifier (Watchtower, Diun, WUD) tells you a new
tag exists. None of them tell you WHAT changed between your version and
the new one. That gap is HomelabSage's wedge.

This module fetches every GitHub/Codeberg release strictly between
`current_version` and `new_version`, concatenates the bodies, and trims
to a sensible length for LLM context. The docker plugin attaches the
result to `Update.context.release_notes_diff`; the prompt rule below
tells the LLM to mine the diff for breaking changes the user actually
crosses by upgrading.

Why concatenate rather than diff-of-diffs:
  - Releases are independent narrative bodies. Diffing the text adds
    noise (formatting churn, link reshuffling) without insight.
  - The LLM is fully capable of summarising 5-10 release bodies; what we
    save vs. raw release_notes is the GUARANTEE that nothing between
    `current` and `new` was skipped. That's the actual deliverable.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from packaging.version import InvalidVersion, Version

from .github import list_releases

log = logging.getLogger(__name__)


# Cap on combined body text we ship to the LLM. The analyzer prompt
# already truncates release_notes at 15K; we keep the diff slightly
# smaller so it never dominates the context budget.
DEFAULT_MAX_CHARS = 12000

# Cap on releases we'll walk through. Some repos publish 100+ patch
# releases (kubernetes, electron) — we don't need every body; the
# 10 most recent strictly-newer-than-current cover the user's real
# upgrade path.
DEFAULT_MAX_RELEASES = 10


@dataclass
class VersionDiff:
    """What the docker plugin attaches to `Update.context.release_notes_diff`."""

    from_version: str
    to_version: str
    releases_walked: int
    body: str
    versions_included: list[str] = field(default_factory=list)
    truncated: bool = False

    def to_context(self) -> dict[str, object]:
        return {
            "from_version": self.from_version,
            "to_version": self.to_version,
            "releases_walked": self.releases_walked,
            "versions_included": self.versions_included,
            "truncated": self.truncated,
            # The body itself is the load-bearing field — it goes straight
            # into the LLM prompt under the `release_notes_diff` block.
            "body": self.body,
        }


def _norm_version(s: str | None) -> str:
    """Strip the leading 'v' and surrounding whitespace, lower-case."""
    if not s:
        return ""
    return s.strip().lstrip("vV")


def _between(current: str, new: str, candidate: str) -> bool:
    """True iff `current < candidate <= new`, with semver tolerance.

    Releases that aren't strict semver (date tags, build suffixes) are
    skipped — the diff path requires comparable versions on both ends.
    Callers can still hand the raw release body to the analyzer; this
    helper just refuses to lie about ordering when it can't be sure.
    """
    try:
        c = Version(_norm_version(current))
        n = Version(_norm_version(new))
        x = Version(_norm_version(candidate))
    except InvalidVersion:
        return False
    return c < x <= n


# Headers vary across upstreams. We strip out everything that looks like
# a "Contributors" / "Full changelog" block — those are pure noise to the
# LLM (a list of GitHub handles).
_NOISE_HEADER_RE = re.compile(
    r"^#{1,6}\s+(contributors?|full changelog|new contributors)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)


def _strip_noise(body: str) -> str:
    """Drop sections that are pure attribution / housekeeping.

    Conservative — we only delete the explicit header markers and a single
    paragraph after each. Anything that doesn't match falls through
    unchanged so we never accidentally swallow a "Breaking changes"
    paragraph the user actually needs.
    """
    if not body:
        return ""
    lines = body.splitlines()
    out: list[str] = []
    skip = False
    for ln in lines:
        if _NOISE_HEADER_RE.match(ln):
            skip = True
            continue
        if skip:
            # Stop skipping when we hit another header or a clearly
            # different paragraph (heuristic: a blank line then content
            # that starts with a header or a bullet).
            if ln.startswith(("#", "- ", "* ")) and not ln.startswith("##### "):
                skip = False
                out.append(ln)
            # Else still inside the contributor block — keep skipping.
            continue
        out.append(ln)
    return "\n".join(out).strip()


async def build_diff(
    repo: str,
    current_version: str,
    new_version: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_releases: int = DEFAULT_MAX_RELEASES,
) -> VersionDiff | None:
    """Fetch the releases between `current_version` and `new_version`.

    Returns None when the repo isn't on the supported forges, when
    versions don't parse, or when no relevant releases exist (single-step
    upgrade with no intermediate releases is the common case for active
    upstreams — the caller already has the latest body via the docker
    plugin's existing `release_notes` field, so a `None` here just
    means "use the existing body").
    """
    if not current_version or not new_version:
        return None
    try:
        Version(_norm_version(current_version))
        Version(_norm_version(new_version))
    except InvalidVersion:
        return None

    try:
        releases = await list_releases(repo, per_page=50)
    except Exception as e:
        log.debug("releases_diff: list_releases failed for %s: %s", repo, e)
        return None
    if not releases:
        return None

    # Filter to STRICTLY between (current, new], walk newest-first.
    relevant: list[dict] = []
    for rel in releases:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        tag = (rel.get("tag_name") or "").strip()
        if not tag:
            continue
        if _between(current_version, new_version, tag):
            relevant.append(rel)
        if len(relevant) >= max_releases:
            break

    if not relevant:
        return None

    versions_included: list[str] = []
    parts: list[str] = []
    total = 0
    truncated = False
    for rel in relevant:
        tag = (rel.get("tag_name") or "").strip()
        body = _strip_noise(rel.get("body") or "").strip()
        if not body:
            continue
        header = f"\n\n## {tag}\n"
        chunk = header + body
        if total + len(chunk) > max_chars:
            remaining = max_chars - total
            if remaining > len(header) + 200:
                # Take a partial chunk so we don't lose the tag entirely.
                parts.append(chunk[:remaining])
                total = max_chars
            truncated = True
            break
        parts.append(chunk)
        versions_included.append(tag)
        total += len(chunk)

    if not parts:
        return None

    return VersionDiff(
        from_version=_norm_version(current_version),
        to_version=_norm_version(new_version),
        releases_walked=len(relevant),
        body="".join(parts).strip(),
        versions_included=versions_included,
        truncated=truncated,
    )
