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
