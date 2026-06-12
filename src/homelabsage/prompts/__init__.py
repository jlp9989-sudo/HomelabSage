"""Built-in LLM prompt templates.

Each prompt lives in its own `.md` file alongside this module so editing
prompt logic doesn't require Python edits. `load_template(name)` reads
and caches the file content; tests can override per-call to exercise
edge cases without touching disk.

The Markdown extension is purely aesthetic — the files are
plain-text format strings using Python's `str.format` (`{placeholder}`).
We keep them as `.md` so editors and code reviewers pick up syntax
highlighting and so the rules read like docs.
"""

from __future__ import annotations

import re
from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent
_CACHE: dict[str, str] = {}


def load_template(name: str) -> str:
    """Return the contents of `<name>.md` from this package. Cached."""
    if name in _CACHE:
        return _CACHE[name]
    path = _PROMPTS_DIR / f"{name}.md"
    text = path.read_text(encoding="utf-8")
    _CACHE[name] = text
    return text


def clear_cache() -> None:
    """Drop every cached template. Tests use this between cases."""
    _CACHE.clear()


# Conditional-rules region markers + the per-rule trigger tag.
_BEGIN = "<!-- BEGIN CONDITIONAL RULES -->"
_END = "<!-- END CONDITIONAL RULES -->"
_WHEN_RE = re.compile(r"<!--\s*when:\s*([a-z0-9_,\s]+?)\s*-->", re.IGNORECASE)
_LEAD_IN = "The following rules apply only because the matching context block is present:"


def assemble(template: str, present_keys: set[str]) -> str:
    """Drop conditional rule blocks whose trigger keys aren't in context.

    Each analyzer rule that only matters when a particular context block
    is present lives between the BEGIN/END markers, tagged with
    `<!-- when: key1,key2 -->`. A rule survives iff at least one of its
    keys is in `present_keys` (the update's `context` keys). Everything
    OUTSIDE the markers — the preamble, the unconditional rules, and the
    Update/notes/release-notes tail — is always kept.

    A 24-rule prompt where ~16 rules are gated on context blocks that a
    typical update doesn't carry was shipping all 24 every time; the
    local 35B then had to ignore ~15 irrelevant (and occasionally
    contradictory) rules per call. Sending only the rules whose data is
    actually present cuts that noise and ~1-2k tokens.

    Returns the template UNCHANGED when the markers are absent (a test
    override or an older template), so callers never break.
    """
    if _BEGIN not in template or _END not in template:
        return template
    head, rest = template.split(_BEGIN, 1)
    region, tail = rest.split(_END, 1)

    # `_WHEN_RE.split` yields [preamble, keys1, body1, keys2, body2, …].
    parts = _WHEN_RE.split(region)
    kept: list[str] = []
    for i in range(1, len(parts), 2):
        keys = {k.strip() for k in parts[i].split(",") if k.strip()}
        if keys & present_keys:
            kept.append(parts[i + 1].strip("\n"))

    head = head.rstrip("\n")
    tail = tail.lstrip("\n")
    if not kept:
        # No conditional rule applies — drop the now-pointless lead-in too.
        head = head.replace(_LEAD_IN, "").rstrip("\n")
        return f"{head}\n\n{tail}"
    return f"{head}\n" + "\n".join(kept) + f"\n\n{tail}"
