"""Update table operations: upsert, get, list, set_status, set_notion_page_id."""

from __future__ import annotations

import builtins
import json
import sqlite3

from .._time import parse_iso
from ..models import (
    Analysis,
    AnalyzedUpdate,
    Severity,
    Update,
    UpdateStatus,
)


def row_to_item(row: sqlite3.Row) -> AnalyzedUpdate:
    """Reconstruct an `AnalyzedUpdate` from a `SELECT * FROM updates` row.

    Old rows (pre-v0.2) lack the `notion_page_id` column entirely; older
    rows also stored timestamps as naive ISO — `parse_iso` upgrades them
    to tz-aware UTC at read time so comparisons against `utcnow()` don't
    raise `TypeError`.
    """
    update = Update(
        source=row["source"],
        subject=row["subject"],
        current_version=row["current_version"],
        new_version=row["new_version"],
        release_url=row["release_url"],
        release_notes=row["release_notes"],
        context=json.loads(row["context_json"]),
    )
    analysis: Analysis | None = None
    if row["analysis_json"]:
        analysis = Analysis.model_validate_json(row["analysis_json"])
    elif row["severity"]:
        analysis = Analysis(severity=Severity(row["severity"]), summary=row["summary"] or "")
    # `notion_page_id` is only present after the migration; older DBs without
    # the column would raise IndexError on key access. sqlite3.Row does not
    # implement __contains__, so `key in row` would iterate values — must
    # check explicitly against keys().
    cols = row.keys()
    page_id = row["notion_page_id"] if "notion_page_id" in cols else None
    return AnalyzedUpdate(
        update=update,
        analysis=analysis,
        status=UpdateStatus(row["status"]),
        detected_at=parse_iso(row["detected_at"]),
        analyzed_at=parse_iso(row["analyzed_at"]),
        notion_page_id=page_id,
    )


class UpdatesMixin:
    """Methods touching the `updates` table. Mix into the `Database` class."""

    _conn: sqlite3.Connection

    def upsert(self, item: AnalyzedUpdate) -> None:
        u = item.update
        a = item.analysis
        # `notion_page_id` is preserved across upserts: if the caller hasn't
        # set it on this AnalyzedUpdate (None), the COALESCE keeps the
        # previously-stored value. The output layer is the only writer.
        self._conn.execute(
            """
            INSERT INTO updates (
                id, source, subject, current_version, new_version,
                release_url, release_notes, context_json,
                severity, summary, analysis_json,
                status, detected_at, analyzed_at, notion_page_id
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                release_url    = excluded.release_url,
                release_notes  = excluded.release_notes,
                context_json   = excluded.context_json,
                severity       = excluded.severity,
                summary        = excluded.summary,
                analysis_json  = excluded.analysis_json,
                status         = excluded.status,
                analyzed_at    = excluded.analyzed_at,
                notion_page_id = COALESCE(excluded.notion_page_id, updates.notion_page_id)
            """,
            (
                item.id, u.source, u.subject, u.current_version, u.new_version,
                u.release_url, u.release_notes, json.dumps(u.context),
                a.severity.value if a else None,
                a.summary if a else None,
                a.model_dump_json() if a else None,
                item.status.value,
                item.detected_at.isoformat(),
                item.analyzed_at.isoformat() if item.analyzed_at else None,
                item.notion_page_id,
            ),
        )

    def set_notion_page_id(self, update_id: str, page_id: str) -> None:
        """Record the Notion page id for an update (idempotent)."""
        self._conn.execute(
            "UPDATE updates SET notion_page_id = ? WHERE id = ?",
            (page_id, update_id),
        )

    def get(self, update_id: str) -> AnalyzedUpdate | None:
        row = self._conn.execute(
            "SELECT * FROM updates WHERE id = ?", (update_id,)
        ).fetchone()
        return row_to_item(row) if row else None

    def list(
        self,
        status: UpdateStatus | None = None,
        source: str | None = None,
        limit: int = 200,
    ) -> list[AnalyzedUpdate]:
        sql = "SELECT * FROM updates WHERE 1=1"
        args: list[object] = []
        if status is not None:
            sql += " AND status = ?"
            args.append(status.value)
        if source is not None:
            sql += " AND source = ?"
            args.append(source)
        sql += " ORDER BY detected_at DESC LIMIT ?"
        args.append(limit)
        return [row_to_item(r) for r in self._conn.execute(sql, args).fetchall()]

    def set_status(self, update_id: str, status: UpdateStatus) -> None:
        # Increment failure_count only on a true status TRANSITION
        # into FAILED. Repeated "set to FAILED" calls on an already-
        # FAILED row don't bump the counter — otherwise the bulk
        # endpoint or a UI double-click silently inflates the
        # recurring-failure audit signal. A genuine retry-then-fail
        # loop will flip APPLIED→FAILED (or NEW→FAILED) and bump.
        if status == UpdateStatus.FAILED:
            self._conn.execute(
                "UPDATE updates SET status = ?, "
                "failure_count = "
                "  CASE WHEN status = 'failed' "
                "       THEN COALESCE(failure_count, 0) "
                "       ELSE COALESCE(failure_count, 0) + 1 END "
                "WHERE id = ?",
                (status.value, update_id),
            )
        else:
            self._conn.execute(
                "UPDATE updates SET status = ? WHERE id = ?",
                (status.value, update_id),
            )

    def set_snooze(self, update_id: str, snooze_until: str | None) -> bool:
        """Set or clear the snooze timestamp.

        `snooze_until` is an ISO 8601 string in UTC; pass None to
        clear. Returns True iff the row exists. The engine + outputs
        suppress updates whose snooze is still in the future.
        """
        cur = self._conn.execute(
            "UPDATE updates SET snooze_until = ? WHERE id = ?",
            (snooze_until or None, update_id),
        )
        return (cur.rowcount or 0) > 0

    def get_snooze(self, update_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT snooze_until FROM updates WHERE id = ?", (update_id,),
        ).fetchone()
        if row is None:
            return None
        return row["snooze_until"] or None

    def clear_all_snoozes(self) -> int:
        """Clear `snooze_until` on every row that has one. Returns count."""
        cur = self._conn.execute(
            "UPDATE updates SET snooze_until = NULL "
            "WHERE snooze_until IS NOT NULL",
        )
        return int(cur.rowcount or 0)

    def list_snoozed(
        self, *, now_iso: str | None = None, limit: int = 200,
    ) -> builtins.list[dict]:
        """List currently-snoozed updates (snooze_until > now).

        `now_iso` is an optional ISO timestamp; defaults to current UTC.
        Comparison is lexicographic on the ISO 8601 strings — valid as
        long as the timestamps are normalised to UTC with a `+00:00`
        or `Z` suffix (which the snooze setter validates).
        """
        from .._time import utcnow
        now_s = now_iso or utcnow().isoformat()
        rows = self._conn.execute(
            "SELECT id, subject, source, snooze_until "
            "FROM updates "
            "WHERE snooze_until IS NOT NULL AND snooze_until > ? "
            "ORDER BY snooze_until ASC LIMIT ?",
            (now_s, limit),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "subject": r["subject"],
                "source": r["source"],
                "snooze_until": r["snooze_until"],
            }
            for r in rows
        ]

    def list_recurring_failures(
        self, *, min_count: int = 2, limit: int = 100,
    ) -> builtins.list[dict]:
        """Updates that have failed at least `min_count` times.

        Returns tiny dicts (id, subject, source, failure_count) sorted
        most-failed-first — the auditor surfaces these as a finding so
        the user can stop bashing the same wall.
        """
        rows = self._conn.execute(
            "SELECT id, subject, source, failure_count "
            "FROM updates "
            "WHERE COALESCE(failure_count, 0) >= ? "
            "ORDER BY failure_count DESC LIMIT ?",
            (min_count, limit),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "subject": r["subject"],
                "source": r["source"],
                "failure_count": int(r["failure_count"] or 0),
            }
            for r in rows
        ]

    def set_user_note(self, update_id: str, note: str) -> bool:
        """Attach a free-text user note. Empty string clears the note.

        Returns True iff the row exists. The note is shown alongside
        the analyzer summary in the UI + exported in `history` CSV.
        """
        cur = self._conn.execute(
            "UPDATE updates SET user_note = ? WHERE id = ?",
            (note or None, update_id),
        )
        return (cur.rowcount or 0) > 0

    def set_starred(self, update_id: str, starred: bool) -> bool:
        """Toggle the bookmark flag. Returns True iff the row existed."""
        cur = self._conn.execute(
            "UPDATE updates SET starred = ? WHERE id = ?",
            (1 if starred else 0, update_id),
        )
        return (cur.rowcount or 0) > 0

    def is_starred(self, update_id: str) -> bool:
        row = self._conn.execute(
            "SELECT starred FROM updates WHERE id = ?", (update_id,),
        ).fetchone()
        if row is None:
            return False
        return bool(row["starred"] or 0)

    def list_starred(self, *, limit: int = 200) -> builtins.list[AnalyzedUpdate]:
        """Return only bookmarked rows, newest-first."""
        rows = self._conn.execute(
            "SELECT * FROM updates WHERE starred = 1 "
            "ORDER BY detected_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [row_to_item(r) for r in rows]

    def get_user_note(self, update_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT user_note FROM updates WHERE id = ?", (update_id,),
        ).fetchone()
        if row is None:
            return None
        return row["user_note"]

    def search(
        self,
        query: str,
        *,
        limit: int = 50,
    ) -> builtins.list[AnalyzedUpdate]:
        """Substring search across subject + summary + breaking-changes JSON.

        SQLite LIKE with `%query%` on each searchable column. Case-
        insensitive (`COLLATE NOCASE`). Returns most-recently-detected
        first, capped at `limit`. Empty / whitespace-only queries return
        an empty list rather than every row — saves the caller a guard.
        """
        q = (query or "").strip()
        if not q:
            return []
        pat = f"%{q}%"
        rows = self._conn.execute(
            """
            SELECT * FROM updates
             WHERE subject LIKE ? COLLATE NOCASE
                OR summary LIKE ? COLLATE NOCASE
                OR analysis_json LIKE ? COLLATE NOCASE
                OR user_note LIKE ? COLLATE NOCASE
             ORDER BY detected_at DESC LIMIT ?
            """,
            (pat, pat, pat, pat, limit),
        ).fetchall()
        return [row_to_item(r) for r in rows]
