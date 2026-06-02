"""Tag-pattern intelligence.

Container tags fall into a handful of schemes:

  - **semver** (`1.2.3`, `v1.2.3`, `1.2.3-rc1`)
  - **calver** (`2026.06.01`, `26.06`)
  - **build_suffix** (LSIO's `version-ls123`, `1.4.0-r2`, `1.4.0+build5`)
  - **digest-like** (sha256 fragments, `sha-abc1234`)
  - **floating** (`latest`, `main`, `edge`, `stable`, `nightly`, `dev`,
    branch-style `release-1.x`)
  - **unknown** — anything that doesn't fit a recognised shape

Diun and WUD treat tag filtering as an explicit user knob (per-image
regex). HomelabSage infers the scheme from the published tag listing so
the user doesn't have to maintain regexes manually. The inferred scheme
feeds two consumers:

  1. The floating-tag detector — skips tags that look like one-off
     digest snapshots when the user normally runs semver.
  2. The analyzer prompt — attaches `tag_pattern` to the Update context
     so the LLM can mention "this image's scheme is calver, your
     `latest` tag has drifted N months behind the dated tags".
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from packaging.version import InvalidVersion, Version

# Per-scheme detectors. Order matters: the first match wins, so put the
# stricter patterns ahead of the looser ones.
_SEMVER_RE = re.compile(r"^v?\d+\.\d+(?:\.\d+)?(?:[.\-+][\w\-]+)?$")
_CALVER_RE = re.compile(r"^(?:20\d{2})(?:[.\-_/](?:0[1-9]|1[0-2])(?:[.\-_/](?:0[1-9]|[12]\d|3[01]))?)?$")
_BUILD_SUFFIX_RE = re.compile(
    r"^v?\d+\.\d+(?:\.\d+)?-(?:ls|build|alpine|debian|ubuntu|focal|jammy|bookworm)[\w.\-]*$",
    re.IGNORECASE,
)
_DIGEST_RE = re.compile(r"^sha[\-_]?[0-9a-f]{6,}$", re.IGNORECASE)

# Common "floating" sentinels. Branch-style tags (release-1.x, master, dev,
# canary, …) are also floating in spirit; we use a permissive heuristic.
_FLOATING_LITERALS = frozenset({
    "latest", "main", "master", "edge", "stable", "nightly", "dev",
    "develop", "canary", "rolling", "head", "snapshot",
})
_FLOATING_PATTERN = re.compile(
    r"^(?:release|branch|track)[\-_/]\d+(?:\.x|\.\*)?$",
    re.IGNORECASE,
)

Scheme = str  # one of: semver / calver / build_suffix / digest / floating / unknown
SCHEMES = ("semver", "calver", "build_suffix", "digest", "floating", "unknown")


def classify_tag(tag: str) -> Scheme:
    """Return the scheme of a single tag. Empty input → 'unknown'."""
    if not tag:
        return "unknown"
    t = tag.strip()
    if not t:
        return "unknown"
    if t.lower() in _FLOATING_LITERALS or _FLOATING_PATTERN.match(t):
        return "floating"
    if _DIGEST_RE.match(t):
        return "digest"
    if _BUILD_SUFFIX_RE.match(t):
        return "build_suffix"
    if _CALVER_RE.match(t):
        return "calver"
    # Strict semver via packaging.Version — handles `1.0.0`, `v1.0`,
    # `1.0.0rc1`. Rejects `cuda12`, `openvino`, etc.
    if _SEMVER_RE.match(t):
        try:
            Version(t.lstrip("vV"))
            return "semver"
        except InvalidVersion:
            pass
    return "unknown"


@dataclass(frozen=True)
class TagSchemeReport:
    """Inferred scheme + how confident we are.

    `dominant` is the most common scheme excluding `floating` and
    `unknown` (the floating tags are real-but-not-trackable; unknown
    bucket is noise). `confidence` is its share of the trackable tags.
    `floating_tags` lists the moving-target tags so callers can decide
    whether to follow them.
    """

    dominant: Scheme
    confidence: float
    counts: dict[Scheme, int]
    floating_tags: list[str]
    total_tags: int


def infer_scheme(tags: list[str]) -> TagSchemeReport:
    """Inspect a list of published tags and pick the dominant scheme.

    Empty / single-tag inputs return a low-confidence "unknown" verdict —
    callers should treat that as "we can't help, fall back to whatever
    behaviour the user had". The detector's job is to be honest about
    its uncertainty.
    """
    if not tags:
        return TagSchemeReport(
            dominant="unknown", confidence=0.0,
            counts={}, floating_tags=[], total_tags=0,
        )
    counts: Counter[Scheme] = Counter()
    floating: list[str] = []
    for t in tags:
        scheme = classify_tag(t)
        counts[scheme] += 1
        if scheme == "floating":
            floating.append(t)

    # Pick dominant excluding floating + unknown. If neither has any
    # entries, fall back to whatever is most common overall (covers the
    # "all-floating" edge case for `:latest`-only images).
    trackable = {s: n for s, n in counts.items() if s not in ("floating", "unknown")}
    if trackable:
        dominant, count = max(trackable.items(), key=lambda kv: kv[1])
        confidence = count / sum(trackable.values())
    elif counts:
        dominant, count = max(counts.items(), key=lambda kv: kv[1])
        confidence = count / sum(counts.values())
    else:
        dominant, confidence = "unknown", 0.0

    return TagSchemeReport(
        dominant=dominant,
        confidence=confidence,
        counts=dict(counts),
        floating_tags=floating,
        total_tags=len(tags),
    )


def matches_scheme(tag: str, scheme: Scheme) -> bool:
    """True iff `tag` fits the given scheme. Used to filter candidate tags
    against the inferred dominant scheme — e.g. when the image's scheme
    is `calver`, an out-of-band `v1.0.0-test` should be skipped."""
    return classify_tag(tag) == scheme
