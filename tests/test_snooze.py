"""Snooze: end-to-end coverage consolidated from 13 release-pinned files.

Snooze touches db helpers, HTTP API, HTML HTMX endpoints, CLI, MCP tools,
engine flush gate, and purge interactions. Previously each surface was
tested in the release file where it shipped (v0.6.8, v0.6.9, v0.7.1,
v0.7.2, v0.7.6, v0.7.9, v0.8.3, v0.9.6, v0.9.7, v0.9.9). Consolidated
here in v0.11.6 — the test history of any individual function is
recoverable via `git log -- tests/test_v0XX.py` if needed.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from homelabsage.cli import app
from homelabsage.config import Config
from homelabsage.db import Database
from homelabsage.models import AnalyzedUpdate, Update, UpdateStatus
from homelabsage.web import create_app

# ─── shared helpers ───────────────────────────────────────────────


def _seed(db, *, source="x", subject="y"):
    item = AnalyzedUpdate(
        update=Update(
            source=source, subject=subject,
            current_version="1", new_version="2",
        ),
    )
    db.upsert(item)
    return item


def _client(tmp_path, *, cfg_path_extra: bool = False):
    """Build a TestClient + DB pair. When `cfg_path_extra` returns the
    config.yaml path too (some HTML-form tests need to write overlays)."""
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    if cfg_path_extra:
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("")
        return (
            TestClient(create_app(cfg, cfg_path)),
            Database(cfg.storage.database_path),
            cfg_path,
        )
    return TestClient(create_app(cfg)), Database(cfg.storage.database_path)


def _config_for_cli(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    db_path = tmp_path / "h.db"
    cfg_path.write_text(
        f"""storage:
  database_path: {db_path}
llm:
  endpoint: ""
""",
    )
    return cfg_path, db_path


# ─── db: set / get / clear ─────────────────────────────────────────


def test_snooze_set_and_get(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    item = _seed(db)
    assert db.get_snooze(item.id) is None
    assert db.set_snooze(item.id, "2026-12-31T00:00:00+00:00") is True
    assert db.get_snooze(item.id) == "2026-12-31T00:00:00+00:00"


def test_snooze_clear(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    item = _seed(db)
    db.set_snooze(item.id, "2026-12-31T00:00:00+00:00")
    assert db.set_snooze(item.id, None) is True
    assert db.get_snooze(item.id) is None


def test_snooze_unknown_update_returns_false(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    assert db.set_snooze("ghost-id", "2026-12-31T00:00:00+00:00") is False
    assert db.get_snooze("ghost-id") is None


# ─── db: list_snoozed + clear_all_snoozes ─────────────────────────


def test_list_snoozed_filters_past(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    a = _seed(db, subject="future")
    b = _seed(db, subject="past")
    db.set_snooze(a.id, "2199-12-31T00:00:00+00:00")
    db.set_snooze(b.id, "2020-01-01T00:00:00+00:00")
    rows = db.list_snoozed()
    assert [r["subject"] for r in rows] == ["future"]


def test_list_snoozed_sorts_by_until(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    a = _seed(db, subject="later")
    b = _seed(db, subject="sooner")
    db.set_snooze(a.id, "2199-12-31T00:00:00+00:00")
    db.set_snooze(b.id, "2050-01-01T00:00:00+00:00")
    rows = db.list_snoozed()
    assert [r["subject"] for r in rows] == ["sooner", "later"]


def test_db_clear_all_snoozes(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    a = _seed(db, subject="a")
    b = _seed(db, subject="b")
    c = _seed(db, subject="c")
    db.set_snooze(a.id, "2199-01-01T00:00:00+00:00")
    db.set_snooze(b.id, "2199-01-01T00:00:00+00:00")
    # c left unsnoozed
    assert db.clear_all_snoozes() == 2
    # idempotent on second call
    assert db.clear_all_snoozes() == 0
    assert db.get_snooze(a.id) is None
    assert db.get_snooze(b.id) is None
    assert db.get_snooze(c.id) is None


# ─── HTTP API: /api/updates/{id}/snooze + /snoozed ────────────────


def test_snooze_api_validates_iso8601(tmp_path):
    client, db = _client(tmp_path)
    item = _seed(db)
    r = client.post(
        f"/api/updates/{item.id}/snooze",
        json={"snooze_until": "not-a-date"},
    )
    assert r.status_code == 400
    r = client.post(
        f"/api/updates/{item.id}/snooze",
        json={"snooze_until": "2026-12-31T00:00:00+00:00"},
    )
    assert r.status_code == 200
    assert r.json()["snooze_until"] == "2026-12-31T00:00:00+00:00"


def test_api_list_snoozed(tmp_path):
    client, db = _client(tmp_path)
    item = _seed(db)
    db.set_snooze(item.id, "2199-12-31T00:00:00+00:00")
    r = client.get("/api/updates/snoozed")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["items"][0]["id"] == item.id


def test_api_delete_snoozed(tmp_path):
    client, db = _client(tmp_path)
    item = _seed(db)
    db.set_snooze(item.id, "2199-01-01T00:00:00+00:00")
    r = client.delete("/api/updates/snoozed")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["cleared"] == 1


# ─── HTML HTMX: /updates/{id}/snooze/quick + /snooze/until ────────


def test_snooze_quick_sets_future_timestamp(tmp_path):
    client, db = _client(tmp_path)
    item = _seed(db, source="docker", subject="mealie")
    r = client.post(
        f"/updates/{item.id}/snooze/quick",
        data={"days": "7"},
    )
    assert r.status_code == 200
    until = db.get_snooze(item.id)
    assert until is not None
    # The future ISO timestamp should start with 202x or later
    assert until[:2] == "20"
    # Response shows the snooze cell with a clear button
    assert "💤" in r.text
    assert "clear" in r.text


def test_snooze_quick_zero_days_clears(tmp_path):
    client, db = _client(tmp_path)
    item = _seed(db, source="docker", subject="mealie")
    db.set_snooze(item.id, "2199-01-01T00:00:00+00:00")
    r = client.post(
        f"/updates/{item.id}/snooze/quick",
        data={"days": "0"},
    )
    assert r.status_code == 200
    assert db.get_snooze(item.id) is None
    # Cleared cell shows the snooze-select form again
    assert "snooze…" in r.text


def test_snooze_quick_default_days_when_unset(tmp_path):
    """No `days` form field → default 7 (FastAPI Form default)."""
    client, db = _client(tmp_path)
    item = _seed(db, source="docker", subject="mealie")
    r = client.post(f"/updates/{item.id}/snooze/quick", data={})
    assert r.status_code == 200
    assert db.get_snooze(item.id) is not None


def test_snooze_quick_404_on_bogus_id(tmp_path):
    client, _db, _cfg = _client(tmp_path, cfg_path_extra=True)
    r = client.post("/updates/does-not-exist/snooze/quick", data={"days": "7"})
    assert r.status_code == 404


def test_snooze_quick_negative_days_clears(tmp_path):
    """days = -5 → treated as 0 (clear), not a past timestamp wedge."""
    client, db, _ = _client(tmp_path, cfg_path_extra=True)
    item = _seed(db, source="docker", subject="mealie")
    db.set_snooze(item.id, "2199-01-01T00:00:00+00:00")
    r = client.post(f"/updates/{item.id}/snooze/quick", data={"days": "-5"})
    assert r.status_code == 200
    assert db.get_snooze(item.id) is None


def test_snooze_until_sets_future_date(tmp_path):
    client, db = _client(tmp_path)
    item = _seed(db, source="docker", subject="mealie")
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    r = client.post(
        f"/updates/{item.id}/snooze/until",
        data={"until": tomorrow},
    )
    assert r.status_code == 200
    until = db.get_snooze(item.id)
    assert until is not None
    assert until.startswith(tomorrow)
    assert "💤" in r.text


def test_snooze_until_blank_clears(tmp_path):
    client, db = _client(tmp_path)
    item = _seed(db, source="docker", subject="mealie")
    db.set_snooze(item.id, "2199-01-01T00:00:00+00:00")
    r = client.post(f"/updates/{item.id}/snooze/until", data={"until": ""})
    assert r.status_code == 200
    assert db.get_snooze(item.id) is None


def test_snooze_until_past_date_clears(tmp_path):
    """Picking today/yesterday is defensive: same as clear."""
    client, db = _client(tmp_path)
    item = _seed(db, source="docker", subject="mealie")
    db.set_snooze(item.id, "2199-01-01T00:00:00+00:00")
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    r = client.post(f"/updates/{item.id}/snooze/until", data={"until": yesterday})
    assert r.status_code == 200
    assert db.get_snooze(item.id) is None


def test_snooze_until_invalid_400(tmp_path):
    client, db = _client(tmp_path)
    item = _seed(db, source="docker", subject="mealie")
    r = client.post(f"/updates/{item.id}/snooze/until", data={"until": "not-a-date"})
    assert r.status_code == 400


def test_snooze_until_404_on_bogus_id(tmp_path):
    client, _db = _client(tmp_path)
    r = client.post("/updates/does-not-exist/snooze/until", data={"until": "2099-01-01"})
    assert r.status_code == 404


def test_index_filter_snoozed(tmp_path):
    client, db = _client(tmp_path)
    a = _seed(db, source="docker", subject="alpha")
    _seed(db, source="docker", subject="beta")
    db.set_snooze(a.id, "2199-01-01T00:00:00+00:00")
    r = client.get("/?filter=snoozed")
    assert r.status_code == 200
    assert "alpha" in r.text
    assert "beta" not in r.text


def test_snoozed_pill_counts_via_count_query(tmp_path):
    client, db, _ = _client(tmp_path, cfg_path_extra=True)
    item = _seed(db, source="docker", subject="alpha")
    db.set_snooze(item.id, "2199-01-01T00:00:00+00:00")
    r = client.get("/")
    assert "Snoozed (1)" in r.text
    assert db.count_snoozed() == 1


# ─── CLI: homelabsage snooze ──────────────────────────────────────


def test_cli_snooze_until(tmp_path):
    cfg_path, db_path = _config_for_cli(tmp_path)
    db = Database(str(db_path))
    item = _seed(db)
    db.close()
    runner = CliRunner()
    result = runner.invoke(
        app, [
            "snooze", item.id, "--config", str(cfg_path),
            "--until", "2199-12-31T00:00:00+00:00",
        ],
    )
    assert result.exit_code == 0, result.stdout
    db = Database(str(db_path))
    assert db.get_snooze(item.id) == "2199-12-31T00:00:00+00:00"


def test_cli_snooze_for_relative(tmp_path):
    cfg_path, db_path = _config_for_cli(tmp_path)
    db = Database(str(db_path))
    item = _seed(db)
    db.close()
    runner = CliRunner()
    result = runner.invoke(
        app, [
            "snooze", item.id, "--config", str(cfg_path),
            "--for", "7d",
        ],
    )
    assert result.exit_code == 0, result.stdout
    db = Database(str(db_path))
    val = db.get_snooze(item.id)
    assert val is not None
    # Crude future check: starts with "2026" or later.
    assert val[:4] >= "2026"


def test_cli_snooze_clear(tmp_path):
    cfg_path, db_path = _config_for_cli(tmp_path)
    db = Database(str(db_path))
    item = _seed(db)
    db.set_snooze(item.id, "2199-12-31T00:00:00+00:00")
    db.close()
    runner = CliRunner()
    result = runner.invoke(
        app, [
            "snooze", item.id, "--config", str(cfg_path), "--clear",
        ],
    )
    assert result.exit_code == 0
    db = Database(str(db_path))
    assert db.get_snooze(item.id) is None


def test_cli_snooze_list(tmp_path):
    cfg_path, db_path = _config_for_cli(tmp_path)
    db = Database(str(db_path))
    item = _seed(db, subject="needs-rev")
    db.set_snooze(item.id, "2199-12-31T00:00:00+00:00")
    db.close()
    runner = CliRunner()
    result = runner.invoke(
        app, ["snooze", "--config", str(cfg_path), "--list"],
    )
    assert result.exit_code == 0
    assert "needs-rev" in result.stdout


def test_cli_snooze_rejects_both_until_and_for(tmp_path):
    cfg_path, db_path = _config_for_cli(tmp_path)
    db = Database(str(db_path))
    item = _seed(db)
    db.close()
    runner = CliRunner()
    result = runner.invoke(
        app, [
            "snooze", item.id, "--config", str(cfg_path),
            "--until", "2199-12-31T00:00:00+00:00",
            "--for", "7d",
        ],
    )
    assert result.exit_code != 0


def test_cli_snooze_rejects_bad_relative(tmp_path):
    cfg_path, db_path = _config_for_cli(tmp_path)
    db = Database(str(db_path))
    item = _seed(db)
    db.close()
    runner = CliRunner()
    result = runner.invoke(
        app, [
            "snooze", item.id, "--config", str(cfg_path),
            "--for", "garbage",
        ],
    )
    assert result.exit_code != 0


# ─── MCP tools: snooze_update / list_snoozed / clear / is_snoozed ──


def test_mcp_snooze_update_validates_iso(tmp_path):
    from homelabsage.mcp import TOOLS
    db = Database(str(tmp_path / "t.db"))
    item = _seed(db)
    cfg = Config()
    impl = TOOLS["snooze_update"]["impl"]
    bad = impl(cfg, db, {"update_id": item.id, "snooze_until": "garbage"})
    assert bad["ok"] is False
    ok = impl(cfg, db, {
        "update_id": item.id,
        "snooze_until": "2026-12-31T00:00:00+00:00",
    })
    assert ok["ok"] is True
    assert ok["snooze_until"] == "2026-12-31T00:00:00+00:00"


def test_mcp_snooze_update_clears_with_null(tmp_path):
    from homelabsage.mcp import TOOLS
    db = Database(str(tmp_path / "t.db"))
    item = _seed(db)
    db.set_snooze(item.id, "2026-12-31T00:00:00+00:00")
    cfg = Config()
    impl = TOOLS["snooze_update"]["impl"]
    out = impl(cfg, db, {"update_id": item.id, "snooze_until": None})
    assert out["ok"] is True
    assert out["snooze_until"] is None


def test_mcp_snooze_update_unknown_id(tmp_path):
    from homelabsage.mcp import TOOLS
    db = Database(str(tmp_path / "t.db"))
    cfg = Config()
    impl = TOOLS["snooze_update"]["impl"]
    out = impl(cfg, db, {"update_id": "ghost",
                         "snooze_until": "2026-12-31T00:00:00+00:00"})
    assert out["ok"] is False


def test_mcp_list_snoozed(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(str(tmp_path / "t.db"))
    item = _seed(db)
    db.set_snooze(item.id, "2199-12-31T00:00:00+00:00")
    impl = TOOLS["list_snoozed"]["impl"]
    out = impl(cfg, db, {})
    assert out["count"] == 1


def test_mcp_clear_all_snoozes(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(str(tmp_path / "t.db"))
    item = _seed(db)
    db.set_snooze(item.id, "2199-01-01T00:00:00+00:00")
    impl = TOOLS["clear_all_snoozes"]["impl"]
    out = impl(cfg, db, {})
    assert out["ok"] is True
    assert out["cleared"] == 1


def test_mcp_is_snoozed_false_when_no_snooze(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(str(tmp_path / "t.db"))
    item = _seed(db)
    impl = TOOLS["is_snoozed"]["impl"]
    out = impl(cfg, db, {"update_id": item.id})
    assert out["snoozed"] is False
    assert out["snooze_until"] is None


def test_mcp_is_snoozed_true_for_future(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(str(tmp_path / "t.db"))
    item = _seed(db)
    db.set_snooze(item.id, "2199-01-01T00:00:00+00:00")
    impl = TOOLS["is_snoozed"]["impl"]
    out = impl(cfg, db, {"update_id": item.id})
    assert out["snoozed"] is True
    assert out["snooze_until"] == "2199-01-01T00:00:00+00:00"


def test_mcp_is_snoozed_false_for_past(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(str(tmp_path / "t.db"))
    item = _seed(db)
    db.set_snooze(item.id, "2020-01-01T00:00:00+00:00")
    impl = TOOLS["is_snoozed"]["impl"]
    out = impl(cfg, db, {"update_id": item.id})
    assert out["snoozed"] is False
    assert out["snooze_until"] == "2020-01-01T00:00:00+00:00"


def test_mcp_is_snoozed_handles_corrupt(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(str(tmp_path / "t.db"))
    item = _seed(db)
    db.set_snooze(item.id, "garbage-not-iso")
    impl = TOOLS["is_snoozed"]["impl"]
    out = impl(cfg, db, {"update_id": item.id})
    assert out["snoozed"] is False
    assert "error" in out


def test_mcp_is_snoozed_requires_update_id():
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["is_snoozed"]["impl"]
    out = impl(cfg, db, {})
    assert out["snoozed"] is False
    assert "error" in out


# ─── engine wiring: _snooze_active_for + flush gate ───────────────


def test_snooze_active_for_returns_iso_when_future(tmp_path):
    from homelabsage.engine import Engine
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    item = _seed(db)
    future = "2199-12-31T00:00:00+00:00"
    db.set_snooze(item.id, future)
    engine = Engine(cfg, db)
    try:
        assert engine._snooze_active_for(item.id) == future
    finally:
        engine.close()


def test_snooze_active_for_past_returns_none(tmp_path):
    from homelabsage.engine import Engine
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    item = _seed(db)
    db.set_snooze(item.id, "2020-01-01T00:00:00+00:00")
    engine = Engine(cfg, db)
    try:
        assert engine._snooze_active_for(item.id) is None
    finally:
        engine.close()


def test_snooze_active_for_corrupt_returns_none(tmp_path):
    """Garbage timestamp must not silence the update permanently."""
    from homelabsage.engine import Engine
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    item = _seed(db)
    db.set_snooze(item.id, "not-a-date")
    engine = Engine(cfg, db)
    try:
        assert engine._snooze_active_for(item.id) is None
    finally:
        engine.close()


def test_snooze_blocks_flush_pending_dispatches(tmp_path):
    """v0.7.1 review fix: queued pushes must skip when snoozed."""
    import asyncio

    from homelabsage.engine import Engine
    from homelabsage.models import Analysis, Severity
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    item = AnalyzedUpdate(
        update=Update(
            source="x", subject="y",
            current_version="1", new_version="2",
        ),
        analysis=Analysis(severity=Severity.HIGH, summary="m"),
    )
    db.upsert(item)

    # Build a stub push output that records calls.
    calls: list[str] = []

    class _StubPush:
        id = "stub"
        is_push = True

        async def send(self, it):
            calls.append(it.id)

    engine = Engine(cfg, db)
    engine.outputs = [_StubPush()]
    try:
        # Queue + snooze into the future
        db.queue_pending_dispatch(item.id, "stub")
        db.set_snooze(item.id, "2199-12-31T00:00:00+00:00")
        asyncio.run(engine._flush_pending_dispatches())
        # snooze in flush MUST suppress the call
        assert calls == []
        # Queue row stays so it retries when snooze expires
        pending = db.list_pending_dispatches()
        assert len(pending) == 1
    finally:
        engine.close()


# ─── interaction: purge skips snoozed ─────────────────────────────


def test_purge_skips_snoozed(tmp_path):
    """Snoozed updates are never purged, even when terminal + old."""
    db = Database(str(tmp_path / "t.db"))
    now = datetime.now(UTC)
    item = AnalyzedUpdate(
        update=Update(
            source="x", subject="snoozy",
            current_version="1", new_version="2",
        ),
        detected_at=(now - timedelta(days=200)),
    )
    db.upsert(item)
    db.set_status(item.id, UpdateStatus.APPLIED)
    db.set_snooze(item.id, "2199-01-01T00:00:00+00:00")
    n = db.purge_old_updates(older_than_days=90)
    assert n == 0


# Keep yaml + json imported so a stray import-prune doesn't break later.
_ = (yaml, json)
