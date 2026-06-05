"""Audit-finding mute list.

When the auditor surfaces a finding the user has consciously decided
to ignore (e.g. a `disk_pressure` warning on a pool they're about to
expand, or a `compose_override` they intentionally maintain), the
user can MUTE that finding by its stable `(category, source_kind,
source_ref)` fingerprint. Muted findings drop out of
`audit.build_report` AND therefore out of the dashboard / Markdown /
audit_history.jsonl persistence.

`expires_at` is an ISO 8601 timestamp; NULL/empty means permanent.
The auditor's filter compares lexicographically vs `utcnow()`, the
same convention `db.list_snoozed` uses.

Bulk-flip semantics are intentionally cheap: muting a noisy category
across every source_ref means N rows, not 1 wildcard — explicit
lists are easier to audit than glob matchers.
"""

from __future__ import annotations

import builtins
import sqlite3

from .._time import utcnow


class AuditMutesMixin:
    """Methods touching the `audit_mutes` table."""

    _conn: sqlite3.Connection

    def add_audit_mute(
        self,
        *,
        category: str,
        source_kind: str,
        source_ref: str,
        expires_at: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Mute one finding fingerprint. Idempotent — re-adding refreshes.

        Empty / None `expires_at` means permanent. The caller is
        responsible for validating the ISO format before passing it
        in (the HTTP/MCP layers do this; tests pass valid strings).
        """
        self._conn.execute(
            """
            INSERT OR REPLACE INTO audit_mutes
                (category, source_kind, source_ref, expires_at, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                category, source_kind, source_ref,
                expires_at or None, reason or None,
                utcnow().isoformat(),
            ),
        )

    def remove_audit_mute(
        self,
        *,
        category: str,
        source_kind: str,
        source_ref: str,
    ) -> bool:
        """Drop one row. Returns True iff a row existed."""
        cur = self._conn.execute(
            "DELETE FROM audit_mutes WHERE category=? AND source_kind=? "
            "AND source_ref=?",
            (category, source_kind, source_ref),
        )
        return (cur.rowcount or 0) > 0

    def list_audit_mutes(
        self, *, include_expired: bool = False,
    ) -> builtins.list[dict]:
        """Return mute rows. Sorted by `created_at` newest first.

        `include_expired=False` (default) drops rows whose
        `expires_at` is in the past.
        """
        if include_expired:
            rows = self._conn.execute(
                "SELECT category, source_kind, source_ref, expires_at, "
                "reason, created_at FROM audit_mutes "
                "ORDER BY created_at DESC",
            ).fetchall()
        else:
            now_s = utcnow().isoformat()
            rows = self._conn.execute(
                "SELECT category, source_kind, source_ref, expires_at, "
                "reason, created_at FROM audit_mutes "
                "WHERE expires_at IS NULL OR expires_at = '' "
                "   OR expires_at > ? "
                "ORDER BY created_at DESC",
                (now_s,),
            ).fetchall()
        return [
            {
                "category": r["category"],
                "source_kind": r["source_kind"],
                "source_ref": r["source_ref"],
                "expires_at": r["expires_at"],
                "reason": r["reason"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def active_audit_mute_keys(self) -> builtins.set[tuple[str, str, str]]:
        """Return the fingerprint set used by `audit.build_report`.

        Optimised path — returns just the tuples for in-membership
        check, no full row payload. Drops expired rows.
        """
        now_s = utcnow().isoformat()
        rows = self._conn.execute(
            "SELECT category, source_kind, source_ref FROM audit_mutes "
            "WHERE expires_at IS NULL OR expires_at = '' "
            "   OR expires_at > ?",
            (now_s,),
        ).fetchall()
        return {
            (r["category"], r["source_kind"], r["source_ref"])
            for r in rows
        }

    def purge_expired_audit_mutes(self) -> int:
        """Drop rows whose `expires_at` is in the past. Returns count.

        Optional cleanup — the in-memory filter ignores expired rows
        anyway, so the only reason to call this is to keep the table
        small after long-running deployments.
        """
        now_s = utcnow().isoformat()
        cur = self._conn.execute(
            "DELETE FROM audit_mutes WHERE expires_at IS NOT NULL "
            "AND expires_at != '' AND expires_at <= ?",
            (now_s,),
        )
        return int(cur.rowcount or 0)


__all__ = ["AuditMutesMixin"]
