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
    """Single connection wrapper. Thread-safe for reads, writes serialised
    at the engine level (one scan at a time)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # `check_same_thread=False`: the connection is created on the main
        # thread (via `create_app`) but closed and read from APScheduler's
        # worker threads and from FastAPI's shutdown event loop. WAL +
        # autocommit (`isolation_level=None`) already make concurrent reads
        # safe; we serialise writes at the engine level.
        self._conn = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        migrate(self._conn)

    def close(self) -> None:
        self._conn.close()


__all__ = ["Database", "_row_to_item", "_row_to_question"]
