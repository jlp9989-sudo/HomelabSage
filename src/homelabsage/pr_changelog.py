"""Pull-request changelog summarizer.

When a repo's release notes are one-liners (`Bump version`, `Release
v2.0.0`) the analyzer can't reason about what changed. This module
fills the gap by fetching the merged PRs between two tags and joining
their titles into a synthetic changelog.

GitHub's API exposes this via `/repos/{slug}/compare/{base}...{head}`
which returns a `commits` array. We walk the commits, extract the
merge-commit PR-number references (`(#1234)` suffix on the subject
line), then optionally fetch each PR's title for the summary. The
intermediate-fetch cost is bounded by `max_prs` so a major-release
gap doesn't fan out into 200 HTTP calls.

Output: `Update.context.pr_changelog` carrying:
  - `commits_in_range` (int)
  - `merge_prs` (list[{number, subject}])

The analyzer's prompt rule mentions when `merge_prs` is non-empty and
the LLM treats each title as a finer-grained changelog entry.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

from .github import _headers, _resolve_api

log = logging.getLogger(__name__)


# `Merge pull request #1234` (classic GitHub UI) OR `(#1234)` suffix
# (squash-merge style). Both forms carry the PR number we want.
_PR_NUMBER_RE = re.compile(r"(?:#(\d+))")


@dataclass
class PRChangelogEntry:
    """One PR in the synthetic changelog."""

    number: int
    subject: str

    def to_context(self) -> dict:
        return {"number": self.number, "subject": self.subject}


@dataclass
class PRChangelog:
    base: str
    head: str
    commits_in_range: int
    entries: list[PRChangelogEntry]

    def to_context(self) -> dict:
        return {
            "base": self.base,
            "head": self.head,
            "commits_in_range": self.commits_in_range,
            "merge_prs": [e.to_context() for e in self.entries],
        }


async def _fetch_compare(
    repo: str, base: str, head: str, *, timeout: float = 20.0,
) -> dict | None:
    api_base, slug = _resolve_api(repo)
    url = f"{api_base}/repos/{slug}/compare/{base}...{head}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, headers=_headers(api_base))
            if r.status_code != 200:
                return None
            return r.json()
    except httpx.HTTPError as e:
        log.debug("compare fetch %s failed: %s", url, e)
        return None


def _extract_pr_refs(commits: list[dict]) -> list[PRChangelogEntry]:
    """Walk commits, pull (#N) references out of the subject line.

    A commit can mention multiple PRs but the FIRST one is by
    convention the merge target — that's what we keep. Subjects beyond
    the first newline are ignored (we want a one-line summary).
    """
    seen: set[int] = set()
    out: list[PRChangelogEntry] = []
    for c in commits:
        if not isinstance(c, dict):
            continue
        commit = c.get("commit") or {}
        if not isinstance(commit, dict):
            continue
        msg = commit.get("message") or ""
        subject = msg.split("\n", 1)[0].strip()
        if not subject:
            continue
        m = _PR_NUMBER_RE.search(subject)
        if not m:
            continue
        try:
            num = int(m.group(1))
        except ValueError:
            continue
        if num in seen:
            continue
        seen.add(num)
        out.append(PRChangelogEntry(number=num, subject=subject))
    return out


async def build_changelog(
    repo: str,
    *,
    base: str,
    head: str,
    max_prs: int = 30,
) -> PRChangelog | None:
    """Fetch + parse the compare endpoint into a `PRChangelog`.

    `base`/`head` are tags or commit SHAs. Returns None on any
    failure path.
    """
    if not base or not head or base == head:
        return None
    payload = await _fetch_compare(repo, base, head)
    if not isinstance(payload, dict):
        return None
    commits = payload.get("commits")
    if not isinstance(commits, list):
        return None
    entries = _extract_pr_refs(commits)[:max_prs]
    return PRChangelog(
        base=base, head=head,
        commits_in_range=len(commits),
        entries=entries,
    )


__all__ = [
    "PRChangelog",
    "PRChangelogEntry",
    "build_changelog",
]
