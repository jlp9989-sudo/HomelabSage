"""Regression tests for the v0.4.7 review-driven fixes.

Each test pins one bug the review found in v0.4.4/v0.4.5/v0.4.6.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from homelabsage import web
from homelabsage.config import load_config
from homelabsage.image_pins import _matches_pin, evaluate
from homelabsage.models import (
    AnalyzedUpdate,
    Update,
)
from homelabsage.rollback import build_recipe

# ─── rollback prior_image must be a valid docker ref shape ──────────


def test_rollback_floating_tag_emits_parseable_ref():
    """Previously: `slug@sha256:abc<full-digest-needed>` — bogus.
    Now: `slug:<previous-digest-needed>` + short digest in own field."""
    item = AnalyzedUpdate(update=Update(
        source="docker", subject="watchtower",
        current_version="local @ abc123def456",
        new_version="registry @ 999abc",
        context={"registry_slug": "containrrr/watchtower"},
    ))
    recipe = build_recipe(item)
    assert "<full-digest-needed>" not in recipe.prior_image
    assert recipe.prior_image.endswith(":<previous-digest-needed>")
    assert recipe.prior_digest_short == "abc123def456"


def test_rollback_semver_unchanged():
    item = AnalyzedUpdate(update=Update(
        source="docker", subject="mealie",
        current_version="1.2.3", new_version="2.0.0",
        context={"image": "ghcr.io/mealie/mealie:2.0.0",
                 "registry_slug": "ghcr.io/mealie/mealie"},
    ))
    recipe = build_recipe(item)
    assert recipe.prior_image == "ghcr.io/mealie/mealie:1.2.3"
    assert recipe.prior_digest_short is None


# ─── image_pins specificity precedence ──────────────────────────────


def test_image_pins_exact_match_wins_over_glob():
    """Even if `*` appears earlier in the dict, the explicit `mealie`
    key must take precedence."""
    out = evaluate(
        subject="mealie",
        new_version="2.0",
        pins={"*": "0.*", "mealie": "1.*"},
    )
    assert out is not None
    assert out.pin == "1.*"
    assert out.pin_subject == "mealie"


def test_image_pins_glob_used_when_no_exact():
    out = evaluate(
        subject="random-svc",
        new_version="2.0",
        pins={"mealie": "1.*", "*": "0.*"},
    )
    assert out is not None
    assert out.pin == "0.*"


# ─── image_pins parse failure should not silently block ─────────────


def test_image_pins_unparseable_version_returns_allowed():
    """`packaging` can't parse a custom shape — must not silently block."""
    ok, reason = _matches_pin("custom-build-xyz", "<=2026.5")
    assert ok
    assert "could not parse" in reason


# ─── notes_git accepts free-form author strings ─────────────────────


def test_notes_git_accepts_bare_author_string(tmp_path: Path):
    import shutil
    if not shutil.which("git"):
        import pytest
        pytest.skip("git not on PATH")
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    f = tmp_path / "x.md"
    f.write_text("hi")
    from homelabsage.notes_git import auto_commit
    # Previously this crashed with IndexError on the split('<')[1] path.
    ok = auto_commit(tmp_path, files=[f], message="m", author="javi")
    assert ok


# ─── bulk API caps and validates input ──────────────────────────────


def _bulk_app(tmp_path: Path):
    body = (
        "llm:\n"
        "  provider: disabled\n"
        "  model: stub\n"
        "  endpoint: http://stub\n"
        "scheduler:\n"
        "  enabled: false\n"
        "storage:\n"
        f"  database_path: {tmp_path}/state.sqlite\n"
        "web:\n"
        "  enabled: true\n"
        "  host: 127.0.0.1\n"
        "  port: 0\n"
        "  auth:\n"
        "    enabled: false\n"
    )
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(body)
    cfg = load_config(cfg_path)
    return web.create_app(cfg, cfg_path=cfg_path)


def test_bulk_rejects_non_list_ids(tmp_path: Path):
    app = _bulk_app(tmp_path)
    with TestClient(app) as client:
        r = client.post("/api/updates/bulk", json={"ids": "abc", "status": "applied"})
        assert r.status_code == 400


def test_bulk_rejects_over_cap(tmp_path: Path):
    app = _bulk_app(tmp_path)
    with TestClient(app) as client:
        r = client.post(
            "/api/updates/bulk",
            json={"ids": [f"x:{i}:1" for i in range(600)], "status": "applied"},
        )
        assert r.status_code == 400
        assert "cap is 500" in r.text


# ─── interview cleanup keeps answered_at NULL ───────────────────────


def test_interview_cleanup_does_not_set_answered_at(tmp_path: Path):
    """Auto-dismissal is not an answer — `answered_at` stays NULL."""
    from datetime import UTC, datetime, timedelta

    from homelabsage.db import Database
    from homelabsage.models import InterviewQuestion
    db = Database(tmp_path / "s.sqlite")
    try:
        q = InterviewQuestion(
            container_name="c", image_digest_short="d",
            question_text="?",
            created_at=datetime.now(UTC) - timedelta(days=60),
        )
        new_id = db.add_interview_question(q)
        db._conn.execute(
            "UPDATE interview_questions SET created_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(days=60)).isoformat(), new_id),
        )
        n = db.dismiss_stale_interview_questions(older_than_days=30)
        assert n == 1
        row = db._conn.execute(
            "SELECT answered_at FROM interview_questions WHERE id = ?",
            (new_id,),
        ).fetchone()
        assert row["answered_at"] is None
    finally:
        db.close()


def test_interview_cleanup_negative_days_is_noop(tmp_path: Path):
    from homelabsage.db import Database
    db = Database(tmp_path / "s.sqlite")
    try:
        assert db.dismiss_stale_interview_questions(older_than_days=-1) == 0
    finally:
        db.close()
