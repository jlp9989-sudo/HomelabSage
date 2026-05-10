"""Notes provider — extra context the user wants the LLM to consider.

Sources:
  - `notes_dir`: directory with .md / .txt files. Each file is scanned and
    matched against the update's `subject` (case-insensitive substring).
  - `extra_docs`: list of files always included verbatim (CLAUDE.md, etc).

Output is plain text injected into the LLM prompt.

This module also exposes `NotesEditor` — CRUD operations on `notes_dir` used
by the web UI. Paths are validated to stay within `notes_dir`.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

ALLOWED_SUFFIXES = {".md", ".txt"}

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


def _match_score(keywords: list[str], header_path: str, body: str) -> int:
    """Rank a section by how strongly it matches ANY of the given keywords.

    Each keyword in the header counts 3×; in the body, raw occurrences.
    Short keywords (<3 chars) are skipped to avoid noise from common letters.
    """
    if not keywords:
        return 0
    hp = header_path.lower()
    bd = body.lower()
    score = 0
    for kw in keywords:
        k = kw.strip().lower()
        if len(k) < 3:
            continue
        if k in hp:
            score += 3
        score += bd.count(k)
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

    def context_for(self, subject: str, keywords: list[str] | None = None) -> str:
        """Return up to `max_chars` of the most relevant notes.

        Matches against `subject` plus any extra `keywords` the plugin provides
        (image repo, compose stack, image short name, etc.).
        """
        terms = [subject] + (keywords or [])
        scored: list[tuple[int, Path, str, str]] = []
        for f, h, b in self._sections:
            s = _match_score(terms, h, b)
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


# ─── Editor (web UI) ──────────────────────────────────────────────────


class NotesEditor:
    """CRUD operations on `notes_dir`, used by the web UI.

    All operations are path-traversal safe: filenames are sanitized and
    resolved paths must stay inside `notes_dir`.
    """

    def __init__(self, notes_dir: str | Path | None):
        self.dir = Path(notes_dir).resolve() if notes_dir else None

    @property
    def enabled(self) -> bool:
        return self.dir is not None and self.dir.is_dir()

    def _safe_path(self, filename: str) -> Path:
        if not self.enabled:
            raise PermissionError("notes_dir is not configured or does not exist")
        # No subdirs, no traversal — only flat files inside notes_dir.
        clean = Path(filename).name
        if not clean or clean.startswith("."):
            raise ValueError(f"invalid filename: {filename!r}")
        if Path(clean).suffix.lower() not in ALLOWED_SUFFIXES:
            raise ValueError(f"only {sorted(ALLOWED_SUFFIXES)} files are allowed")
        full = (self.dir / clean).resolve()  # type: ignore[union-attr]
        if not str(full).startswith(str(self.dir)):  # type: ignore[union-attr]
            raise PermissionError(f"path escapes notes_dir: {filename!r}")
        return full

    def list(self) -> list[dict]:
        if not self.enabled:
            return []
        out: list[dict] = []
        for p in sorted(self.dir.glob("*")):  # type: ignore[union-attr]
            if p.is_file() and p.suffix.lower() in ALLOWED_SUFFIXES:
                stat = p.stat()
                out.append({
                    "name": p.name,
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                })
        return out

    def read(self, filename: str) -> str:
        return self._safe_path(filename).read_text(encoding="utf-8", errors="replace")

    def write(self, filename: str, content: str) -> None:
        path = self._safe_path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write — temp then rename to avoid half-written files.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)

    def delete(self, filename: str) -> None:
        self._safe_path(filename).unlink(missing_ok=True)
