"""HomelabSage SQLite layer.

Mixin-composed `Database` class: each per-domain module ships a `*Mixin`
exposing its slice of the API. The class here owns connection lifecycle
(open, PRAGMA, schema bootstrap, migrations, close); the topic mixins
own the SQL.

Public surface (kept stable for `from homelabsage.db import Database`):

    Database(path)     → opens an idempotent connection + runs migrations
        .close()
        # updates table
        .upsert/get/list/set_status/set_notion_page_id
        # interview_questions
        .add/list/get/answer/dismiss/count_interview_question(s)
        # watched_repos
        .add/list/get/toggle/remove/update_watched_repo_version
        # pending_dispatches
        .queue/list/delete_pending_dispatch
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .audit_mutes import AuditMutesMixin
from .explainers import ExplainersMixin
from .health import HealthCheckMixin
from .heartbeats import HeartbeatsMixin
from .interview import InterviewMixin
from .interview import row_to_question as _row_to_question
from .log_anom import LogAnomalyMixin
from .pending import PendingMixin
from .schema import SCHEMA, migrate
from .updates import UpdatesMixin

# Re-exported for back-compat with `from homelabsage.db import _row_to_item`
# which a handful of tests + the curator referenced in earlier versions.
from .updates import row_to_item as _row_to_item
from .usage import UsageMixin
from .watched import WatchedMixin


class Database(
    UpdatesMixin, InterviewMixin, WatchedMixin, PendingMixin,
    ExplainersMixin, UsageMixin, HealthCheckMixin, LogAnomalyMixin,
    HeartbeatsMixin, AuditMutesMixin,
):
    """Single connection wrapper. WAL + autocommit make single-statement
    reads and writes safe across threads. For multi-statement writes that
    must be atomic, use `db.transaction()` — it acquires an `RLock` AND
    opens a BEGIN/COMMIT, so two threads can't observe a half-applied
    composite write.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # `check_same_thread=False`: the connection is created on the main
        # thread (via `create_app`) but closed and read from APScheduler's
        # worker threads and from FastAPI's `to_thread` workers. WAL +
        # autocommit make single-statement ops safe; the lock below
        # protects multi-statement transactions started via
        # `Database.transaction()`.
        self._conn = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        migrate(self._conn)
        # Reentrant so a transaction body that calls another helper which
        # also tries to acquire the lock doesn't deadlock. SQLite is the
        # ultimate serializer; the lock just guarantees Python-level
        # serialisation of multi-statement ops with mid-transaction reads.
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Atomic multi-statement write.

        Usage:
            with db.transaction() as conn:
                conn.execute("UPDATE …")
                conn.execute("INSERT …")

        Holds the per-connection `RLock` for the duration so two threads
        can't interleave statements, AND wraps the body in `BEGIN
        IMMEDIATE` / `COMMIT` (autocommit is off inside the block).
        Re-raising any exception causes a `ROLLBACK`.

        Read-only paths don't need this — SQLite serialises individual
        statements internally. Use only when you have two or more
        write/read-modify-write statements that must commit together.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def close(self) -> None:
        self._conn.close()


__all__ = ["Database", "_row_to_item", "_row_to_question"]
