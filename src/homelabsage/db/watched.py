"""Watched-repos CRUD — user-curated GitHub/Codeberg repos that aren't containers."""

from __future__ import annotations

import sqlite3

from .._time import utcnow


class WatchedMixin:
    """Methods touching the `watched_repos` table."""

    _conn: sqlite3.Connection

    def add_watched_repo(
        self,
        repo: str,
        *,
        nickname: str | None = None,
        current_version: str | None = None,
    ) -> int:
        """Insert or revive a watched repo. Returns the row id.

        If the repo already exists, the existing row is flipped to active and
        the nickname / current_version are updated when the caller supplies
        them. This makes `homelabsage watched add owner/repo` idempotent:
        running it twice never creates a duplicate.
        """
        existing = self._conn.execute(
            "SELECT id, nickname, current_version FROM watched_repos WHERE repo = ?",
            (repo,),
        ).fetchone()
        if existing is not None:
            self._conn.execute(
                """
                UPDATE watched_repos
                   SET active = 1,
                       nickname = COALESCE(?, nickname),
                       current_version = COALESCE(?, current_version)
                 WHERE id = ?
                """,
                (nickname, current_version, existing["id"]),
            )
            return int(existing["id"])
        cursor = self._conn.execute(
            """
            INSERT INTO watched_repos
                (repo, nickname, current_version, active, added_at)
            VALUES (?, ?, ?, 1, ?)
            """,
            (repo, nickname, current_version, utcnow().isoformat()),
        )
        return int(cursor.lastrowid or 0)

    def list_watched_repos(self, *, active_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM watched_repos"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY added_at DESC"
        return [dict(r) for r in self._conn.execute(sql).fetchall()]

    def get_watched_repo(self, watched_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM watched_repos WHERE id = ?", (watched_id,)
        ).fetchone()
        return dict(row) if row else None

    def toggle_watched_repo(self, watched_id: int, active: bool) -> None:
        self._conn.execute(
            "UPDATE watched_repos SET active = ? WHERE id = ?",
            (1 if active else 0, watched_id),
        )

    def remove_watched_repo(self, watched_id: int) -> None:
        self._conn.execute(
            "DELETE FROM watched_repos WHERE id = ?", (watched_id,)
        )

    def update_watched_repo_version(self, watched_id: int, version: str) -> None:
        self._conn.execute(
            "UPDATE watched_repos SET current_version = ? WHERE id = ?",
            (version, watched_id),
        )
