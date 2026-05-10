"""Notes provider — extra context the user wants the LLM to consider.

Sources:
  - `notes_dir`: directory with .md / .txt files. Each file is scanned and
    matched against the update's `subject` (case-insensitive substring).
  - `extra_docs`: list of files always included verbatim (CLAUDE.md, etc).

Output is plain text injected into the LLM prompt.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# Match a Markdown header line: "## Foo" or "### Bar"
_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log.warning("notes: cannot read %s: %s", path, e)
        return ""


def _sections(md: str) -> list[tuple[str, str]]:
    """Split a Markdown doc into `(header_path, section_body)` chunks.

    Section body covers from a header until the next same-or-higher-level header.
    `header_path` is the concatenated chain ("Top > Sub > Subsub").
    Non-headed prologue (if any) appears as ("", body).
    """
    lines = md.splitlines()
    sections: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []  # (level, title)
    buf: list[str] = []

    def flush() -> None:
        if buf:
            path = " > ".join(t for _, t in stack)
            sections.append((path, "\n".join(buf).strip()))
            buf.clear()

    for raw in lines:
        m = _HEADER_RE.match(raw)
        if m:
            flush()
            level = len(m.group(1))
            title = m.group(2)
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            continue
        buf.append(raw)
    flush()
    return sections


def _match_score(subject: str, header_path: str, body: str) -> int:
    """Simple ranking — substring presence in header (weight 3) or body (1)."""
    s = subject.lower()
    score = 0
    if s in header_path.lower():
        score += 3
    score += body.lower().count(s)
    return score


class NotesProvider:
    """Loads notes once at construction; cheap per-update lookup."""

    def __init__(
        self,
        notes_dir: str | Path | None = None,
        extra_docs: list[str | Path] | None = None,
        max_chars: int = 4000,
    ):
        self.max_chars = max_chars
        self._sections: list[tuple[Path, str, str]] = []  # (file, header_path, body)
        self._always: list[str] = []

        if notes_dir:
            d = Path(notes_dir)
            if d.is_dir():
                for p in sorted(d.glob("**/*")):
                    if p.suffix.lower() in {".md", ".txt"} and p.is_file():
                        for h, b in _sections(_read_text(p)):
                            if b:
                                self._sections.append((p, h, b))

        for ref in extra_docs or []:
            p = Path(ref)
            if p.is_file():
                text = _read_text(p)
                if text:
                    self._always.append(f"### {p.name}\n{text}")

    def context_for(self, subject: str) -> str:
        """Return up to `max_chars` of the most relevant notes for `subject`."""
        scored: list[tuple[int, Path, str, str]] = []
        for f, h, b in self._sections:
            s = _match_score(subject, h, b)
            if s > 0:
                scored.append((s, f, h, b))
        scored.sort(key=lambda x: x[0], reverse=True)

        parts: list[str] = []
        budget = self.max_chars

        # Always-included docs first
        for blob in self._always:
            if budget <= 0:
                break
            parts.append(blob[: budget])
            budget -= len(parts[-1])

        # Then matched sections, best first
        for _score, f, h, b in scored:
            if budget <= 0:
                break
            header = f"### {f.name} — {h}" if h else f"### {f.name}"
            chunk = f"{header}\n{b}"[: budget]
            parts.append(chunk)
            budget -= len(chunk)

        return "\n\n".join(parts)
