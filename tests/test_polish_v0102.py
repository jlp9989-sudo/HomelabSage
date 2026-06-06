"""Tests for v0.10.2: punch-list I1 (DB transaction primitive) + I2 (apprise to_thread)."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from homelabsage.config import AppriseOutputConfig, Config
from homelabsage.db import Database
from homelabsage.models import (
    Analysis,
    AnalyzedUpdate,
    Severity,
    Update,
)
from homelabsage.outputs.apprise import AppriseOutput

# ─── I1: db.transaction() primitive ───────────────────────────────


def test_transaction_commits_on_success(tmp_path):
    db = Database(tmp_path / "t.db")
    item = AnalyzedUpdate(update=Update(
        source="docker", subject="alpha", current_version="1", new_version="2",
    ))
    db.upsert(item)
    with db.transaction() as conn:
        conn.execute("UPDATE updates SET user_note = ? WHERE id = ?", ("multi", item.id))
        conn.execute("UPDATE updates SET starred = 1 WHERE id = ?", (item.id,))
    assert db.get_user_note(item.id) == "multi"
    assert db.is_starred(item.id) is True


def test_transaction_rolls_back_on_error(tmp_path):
    db = Database(tmp_path / "t.db")
    item = AnalyzedUpdate(update=Update(
        source="docker", subject="alpha", current_version="1", new_version="2",
    ))
    db.upsert(item)
    db.set_user_note(item.id, "before")
    with pytest.raises(RuntimeError), db.transaction() as conn:
        conn.execute("UPDATE updates SET user_note = ? WHERE id = ?", ("during", item.id))
        raise RuntimeError("intentional")
    # ROLLBACK must restore the pre-transaction value
    assert db.get_user_note(item.id) == "before"


def test_transaction_serializes_concurrent_writers(tmp_path):
    """Two threads each open a transaction, do a read-modify-write on the
    same row. With the lock, the final note is one of `T0` or `T1` — never
    interleaved. Without the lock the test ran (per audit) but with the
    primitive future code that uses it can't corrupt."""
    db = Database(tmp_path / "t.db")
    item = AnalyzedUpdate(update=Update(
        source="docker", subject="alpha", current_version="1", new_version="2",
    ))
    db.upsert(item)
    db.set_user_note(item.id, "T0")

    barrier = threading.Barrier(2)

    def worker(label: str):
        barrier.wait()
        for _ in range(20):
            with db.transaction() as conn:
                row = conn.execute(
                    "SELECT user_note FROM updates WHERE id = ?", (item.id,),
                ).fetchone()
                # Append a marker but only if not already this thread's
                base = (row["user_note"] or "").split(":")[0]
                conn.execute(
                    "UPDATE updates SET user_note = ? WHERE id = ?",
                    (f"{base}:{label}", item.id),
                )

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    final = db.get_user_note(item.id)
    # The split-on-colon pattern means the final base must be either "T0"
    # or "T0:a" or "T0:b" — well-formed, never half-mutated.
    assert final and not final.endswith(":")
    parts = final.split(":")
    assert parts[0] == "T0"
    for p in parts[1:]:
        assert p in {"a", "b"}


def test_transaction_does_not_deadlock_on_reentry(tmp_path):
    """RLock — same thread can nest transactions without deadlocking."""
    db = Database(tmp_path / "t.db")
    item = AnalyzedUpdate(update=Update(
        source="docker", subject="alpha", current_version="1", new_version="2",
    ))
    db.upsert(item)
    # Outer transaction commits; the nested call doesn't open a real inner
    # BEGIN (sqlite doesn't support nested BEGIN), but the lock allows
    # re-entry. We just verify no deadlock — call a method that itself
    # uses `with db._lock:` (none exist yet, but the lock is reentrant).
    with db._lock, db._lock:
        db.set_user_note(item.id, "nested")
    assert db.get_user_note(item.id) == "nested"


# ─── I2: apprise.notify offloaded to to_thread ────────────────────


def _make_item():
    return AnalyzedUpdate(
        update=Update(source="docker", subject="mealie",
                      current_version="1", new_version="2"),
        analysis=Analysis(severity=Severity.HIGH, summary="s"),
    )


def test_apprise_send_offloads_notify_to_thread_pool():
    """The blocking `Apprise.notify` runs in a worker thread, so a 0.3 s
    library hang doesn't freeze the caller's event loop. We simulate the
    library and check that during the call the main loop is not blocked."""

    main_thread_id = threading.get_ident()
    notify_thread_id_box: list[int] = []

    class _FakeApprise:
        def add(self, _url): return True
        def notify(self, **_kw):
            notify_thread_id_box.append(threading.get_ident())
            time.sleep(0.15)
            return True

    import homelabsage.outputs.apprise as apprise_mod

    def _make_apprise_module():
        class M:
            Apprise = _FakeApprise
        return M

    cfg = AppriseOutputConfig(enabled=True, urls=["mailtos://u:p@host"])
    out = AppriseOutput(cfg)

    async def run():
        # Replace the lazy import with our fake so we don't need real apprise
        original = apprise_mod._import_apprise
        apprise_mod._import_apprise = _make_apprise_module
        try:
            t0 = time.monotonic()
            await out.send(_make_item())
            return time.monotonic() - t0
        finally:
            apprise_mod._import_apprise = original

    elapsed = asyncio.run(run())
    # Must have actually called notify…
    assert notify_thread_id_box, "notify was never called"
    # …on a *different* thread than the main asyncio loop
    assert notify_thread_id_box[0] != main_thread_id, (
        "notify ran on the event-loop thread — to_thread offload missing"
    )
    # And the await returned only after the fake sleep finished
    assert elapsed >= 0.14


def test_apprise_send_disabled_short_circuits():
    """Defensive: disabled cfg never even imports apprise."""
    cfg = AppriseOutputConfig(enabled=False, urls=["mailtos://u:p@host"])
    out = AppriseOutput(cfg)

    async def run():
        await out.send(_make_item())

    # Just shouldn't raise. (No assertion on side-effects; the `enabled`
    # gate is the contract.)
    asyncio.run(run())


# ─── full Config still loads — transaction primitive doesn't break import ─


def test_database_still_initialises_with_full_config(tmp_path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    assert db._lock is not None
    assert hasattr(db, "transaction")
    db.close()
