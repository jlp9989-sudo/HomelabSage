"""Tests for the watched-repos plugin + DB layer.

Exercises:
  - DB CRUD + idempotent add semantics
  - scan() flow with monkeypatched github helpers
  - first-scan version seeding (no phantom Update on add)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from homelabsage.db import Database
from homelabsage.plugins import github_watched as gw_mod
from homelabsage.plugins.github_watched import WatchedReposPlugin


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "state.sqlite")
    yield d
    d.close()


def test_add_watched_repo_creates_row(db: Database):
    wid = db.add_watched_repo("kyuz0/amd-strix-halo-toolboxes", nickname="halo")
    assert wid > 0
    rows = db.list_watched_repos()
    assert len(rows) == 1
    assert rows[0]["repo"] == "kyuz0/amd-strix-halo-toolboxes"
    assert rows[0]["nickname"] == "halo"
    assert rows[0]["active"] == 1


def test_add_is_idempotent_and_revives_inactive(db: Database):
    wid = db.add_watched_repo("owner/x", nickname="n1")
    db.toggle_watched_repo(wid, active=False)
    # Re-adding revives + can update nickname
    wid2 = db.add_watched_repo("owner/x", nickname="n2")
    assert wid2 == wid
    row = db.get_watched_repo(wid)
    assert row["active"] == 1
    assert row["nickname"] == "n2"


def test_add_preserves_existing_nickname_when_none_passed(db: Database):
    wid = db.add_watched_repo("owner/x", nickname="kept")
    db.add_watched_repo("owner/x")  # no nickname
    assert db.get_watched_repo(wid)["nickname"] == "kept"


def test_list_active_only_filters_inactive(db: Database):
    a = db.add_watched_repo("owner/a")
    db.add_watched_repo("owner/b")
    db.toggle_watched_repo(a, False)
    assert {r["repo"] for r in db.list_watched_repos(active_only=True)} == {"owner/b"}
    assert {r["repo"] for r in db.list_watched_repos(active_only=False)} == {"owner/a", "owner/b"}


def test_remove_drops_row(db: Database):
    wid = db.add_watched_repo("owner/x")
    db.remove_watched_repo(wid)
    assert db.get_watched_repo(wid) is None


# ─── plugin behaviour ────────────────────────────────────────────────────


async def test_scan_skips_when_no_active_rows(db: Database):
    db.add_watched_repo("owner/x")
    db.toggle_watched_repo(1, False)
    plugin = WatchedReposPlugin(db)
    assert await plugin.scan() == []


async def test_scan_seeds_version_on_first_run_no_update_emitted(monkeypatch, db: Database):
    """First scan with no seeded version → set current_version, emit nothing."""
    db.add_watched_repo("owner/x")

    async def fake_latest(repo, **kw):
        return {"tag_name": "v1.0.0", "html_url": "http://example/r/v1.0.0", "body": ""}
    monkeypatch.setattr(gw_mod, "latest_release", fake_latest)

    plugin = WatchedReposPlugin(db)
    updates = await plugin.scan()
    assert updates == []
    # Seeded the row in-place
    assert db.list_watched_repos()[0]["current_version"] == "1.0.0"


async def test_scan_emits_update_when_newer_release(monkeypatch, db: Database):
    db.add_watched_repo("owner/x", current_version="1.0.0")

    async def fake_latest(repo, **kw):
        return {"tag_name": "v1.2.0", "html_url": "http://example/r", "body": "## changes\n- did X"}

    async def fake_metadata(repo, **kw):
        return {"pushed_at": "2026-05-01T00:00:00Z", "archived": False, "html_url": "http://x"}

    monkeypatch.setattr(gw_mod, "latest_release", fake_latest)
    monkeypatch.setattr(gw_mod, "repo_metadata", fake_metadata)

    plugin = WatchedReposPlugin(db)
    updates = await plugin.scan()
    assert len(updates) == 1
    u = updates[0]
    assert u.source == "github_watched"
    assert u.current_version == "1.0.0"
    assert u.new_version == "1.2.0"
    assert "owner/x" in (u.context.get("_note_keywords") or [])


async def test_scan_skips_when_no_newer_release(monkeypatch, db: Database):
    db.add_watched_repo("owner/x", current_version="1.2.0")
    monkeypatch.setattr(gw_mod, "latest_release",
                        lambda repo, **kw: _async({"tag_name": "v1.2.0"}))
    plugin = WatchedReposPlugin(db)
    assert await plugin.scan() == []


async def test_scan_falls_back_to_string_compare_for_date_tags(monkeypatch, db: Database):
    """Date-style tags like `2026.06.01` aren't valid Version() — but the plugin
    still surfaces them when the strings differ."""
    db.add_watched_repo("owner/datey", current_version="2026.05.01")

    async def fake_latest(repo, **kw):
        return {"tag_name": "2026.06.01", "html_url": "", "body": ""}
    monkeypatch.setattr(gw_mod, "latest_release", fake_latest)
    monkeypatch.setattr(gw_mod, "repo_metadata", lambda r, **kw: _async(None))

    plugin = WatchedReposPlugin(db)
    out = await plugin.scan()
    assert len(out) == 1


async def test_scan_survives_github_failures(monkeypatch, db: Database):
    """A failing latest_release on one repo must not derail the next."""
    db.add_watched_repo("owner/broken", current_version="1.0")
    db.add_watched_repo("owner/working", current_version="1.0")

    async def fake_latest(repo, **kw):
        if "broken" in repo:
            raise RuntimeError("rate limited")
        return {"tag_name": "v2.0.0", "html_url": "", "body": ""}

    monkeypatch.setattr(gw_mod, "latest_release", fake_latest)
    monkeypatch.setattr(gw_mod, "repo_metadata", lambda r, **kw: _async(None))

    out = await WatchedReposPlugin(db).scan()
    assert len(out) == 1
    assert out[0].subject == "owner/working"


async def _async(value):
    return value
