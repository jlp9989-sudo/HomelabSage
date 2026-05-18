"""Incremental hook — append analyzer findings to the curator's note.

When the analyzer concludes an update is risky (`recommended_action=hold`)
or carries breaking changes, the analyzer's job ends with a database row
and (optionally) a Telegram/Notion message. The note that documents the
container, written by the curator, stays frozen until the next manual
recurate.

This hook closes that loop: after a risky verdict, we append a single
line to the corresponding `.md` so the note accumulates "trap history"
without anyone having to remember to update it.

Constraints:
  * Only ever modify notes the *curator* wrote — files with the
    `<!-- curator: name@digest -->` footer. A manual note without a
    footer is the user's territory and we leave it alone.
  * Idempotent. The same `(subject, new_version)` must never produce a
    second line if the cron re-runs and the row is still in the DB.
  * Bounded. The log section is capped at `max_lines`; the oldest entries
    drop off the top so the section never grows forever.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from ..models import AnalyzedUpdate
from .helpers import FOOTER_RE, safe_filename

log = logging.getLogger(__name__)

# Markers that delimit the appended block. Keeping them as HTML comments
# means they render invisible in any Markdown viewer.
SECTION_HEADER = "## Update log"
LOG_START = "<!-- update-log:start -->"
LOG_END = "<!-- update-log:end -->"

# Per-line dedup token. The version we tracked is encoded inside an HTML
# comment at the end of the line so the user sees a clean bullet but the
# hook can find existing lines on re-runs.
_LINE_TOKEN_RE = re.compile(r"<!--\s*update:([^\s>-]+)\s*-->")


def _should_log(analyzed: AnalyzedUpdate) -> bool:
    """True if this analyzed update is worth pinning to the note.

    Two triggers: explicit `hold` recommendation OR any item in
    `breaking_changes`. Both are signals the user will want to remember
    next time they touch this service.
    """
    a = analyzed.analysis
    if a is None:
        return False
    if (a.recommended_action or "").strip().lower() == "hold":
        return True
    return bool(a.breaking_changes)


def _format_line(analyzed: AnalyzedUpdate, *, now: datetime | None = None) -> str:
    """Compose the one-line bullet that gets appended.

    Shape:
        - 📌 2026-05-18 — 11.0.13 → 11.0.14 — HOLD: <summary truncated> <!-- update:11.0.14 -->

    The summary is taken from `analysis.summary` if present, falling back
    to the first breaking-change item, truncated so the line stays under
    ~160 chars in most cases (longer summaries are still allowed — the
    Markdown line is just one logical bullet either way).
    """
    a = analyzed.analysis
    assert a is not None
    when = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
    label = "HOLD" if (a.recommended_action or "").strip().lower() == "hold" else "BREAKING"
    summary = (a.summary or "").strip()
    if not summary and a.breaking_changes:
        summary = a.breaking_changes[0].strip()
    if len(summary) > 140:
        summary = summary[:137] + "…"
    version_arrow = f"{analyzed.update.current_version} → {analyzed.update.new_version}"
    token = f"<!-- update:{analyzed.update.new_version} -->"
    return f"- 📌 {when} — {version_arrow} — {label}: {summary} {token}".rstrip()


def _split_at_footer(text: str) -> tuple[str, str]:
    """Return `(body, footer)` so we can re-attach the curator footer after edits.

    The footer is whatever the regex matches (with surrounding whitespace
    preserved on the right side); if no footer, `footer` is empty and the
    caller treats the file as manual.
    """
    m = FOOTER_RE.search(text)
    if not m:
        return text, ""
    return text[: m.start()].rstrip() + "\n", text[m.start() :]


def _has_existing_line(block: str, new_version: str) -> bool:
    """True if a line for this `new_version` already exists in the log block."""
    for line in block.splitlines():
        m = _LINE_TOKEN_RE.search(line)
        if m and m.group(1) == new_version:
            return True
    return False


def _replace_or_insert_block(body: str, new_line: str, *, max_lines: int) -> str:
    """Insert `new_line` into the update-log block, creating it if missing.

    Newest line goes on top so the user sees recent traps first. Cap the
    block at `max_lines` — drop the oldest tail entries.
    """
    start = body.find(LOG_START)
    end = body.find(LOG_END)
    if start != -1 and end != -1 and end > start:
        before = body[:start]
        after = body[end + len(LOG_END):]
        inner = body[start + len(LOG_START):end]
        existing_lines = [ln for ln in inner.splitlines() if ln.strip()]
        existing_lines = [ln for ln in existing_lines if not _LINE_TOKEN_RE.search(ln)
                          or _LINE_TOKEN_RE.search(ln).group(1) != _line_version(new_line)]
        lines = [new_line] + existing_lines
        lines = lines[:max_lines]
        rebuilt = LOG_START + "\n" + "\n".join(lines) + "\n" + LOG_END
        return f"{before}{rebuilt}{after}"
    # No existing block — append a fresh one before the trailing footer split.
    block = f"\n\n{SECTION_HEADER}\n{LOG_START}\n{new_line}\n{LOG_END}\n"
    return body.rstrip() + block


def _line_version(line: str) -> str | None:
    m = _LINE_TOKEN_RE.search(line)
    return m.group(1) if m else None


def append_update_to_note(
    notes_dir: str | Path,
    analyzed: AnalyzedUpdate,
    *,
    max_lines: int = 20,
    now: datetime | None = None,
) -> Path | None:
    """Append an analyzer finding to the curator's note for this subject.

    Returns the path that was written, or None when the hook decided to
    do nothing (no risk signal, no note, no curator footer, already
    logged this version, etc).

    Pure I/O — no LLM call, no network. Safe to call inside the engine
    loop without slowing the cron job.
    """
    if not _should_log(analyzed):
        return None
    try:
        notes_path = Path(notes_dir)
    except Exception:
        return None
    if not notes_path.is_dir():
        return None
    candidate = notes_path / safe_filename(analyzed.update.subject)
    if not candidate.exists():
        return None

    try:
        original = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log.warning("incremental: cannot read %s: %s", candidate, e)
        return None

    body, footer = _split_at_footer(original)
    if not footer:
        # Manual note — never overwrite the user's content.
        return None

    if _has_existing_line(body, analyzed.update.new_version):
        return None

    new_line = _format_line(analyzed, now=now)
    new_body = _replace_or_insert_block(body, new_line, max_lines=max_lines)
    final = new_body.rstrip() + "\n\n" + footer.lstrip() if not footer.startswith("\n") else new_body.rstrip() + "\n" + footer
    final = final.rstrip() + "\n"

    tmp = candidate.with_suffix(candidate.suffix + ".tmp")
    try:
        tmp.write_text(final, encoding="utf-8")
        tmp.replace(candidate)
    except OSError as e:
        log.warning("incremental: cannot write %s: %s", candidate, e)
        return None
    return candidate
