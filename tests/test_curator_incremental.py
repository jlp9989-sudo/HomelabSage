"""Tests for the analyzer→curator incremental hook.

Covers: trigger conditions, line shape, idempotency, max_lines cap,
respect for manual notes (no footer), missing files / dirs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from homelabsage.curator.incremental import (
    LOG_END,
    LOG_START,
    SECTION_HEADER,
    _format_line,
    _has_existing_line,
    _replace_or_insert_block,
    _should_log,
    append_update_to_note,
)
from homelabsage.models import (
    Analysis,
    AnalyzedUpdate,
    Severity,
    Update,
    UpdateStatus,
)

FIXED_NOW = datetime(2026, 5, 18, 12, 0, 0, tzinfo=UTC)


def _analyzed(
    *,
    subject: str = "tintes",
    current: str = "11.0.13",
    new: str = "11.0.14",
    severity: Severity = Severity.HIGH,
    breaking: list[str] | None = None,
    action: str | None = "hold",
    summary: str = "Major DB migration; data must be backed up.",
) -> AnalyzedUpdate:
    return AnalyzedUpdate(
        update=Update(
            source="docker",
            subject=subject,
            current_version=current,
            new_version=new,
        ),
        analysis=Analysis(
            severity=severity,
            summary=summary,
            breaking_changes=breaking or [],
            recommended_action=action,
        ),
        status=UpdateStatus.ANALYZED,
    )


# ─── _should_log ──────────────────────────────────────────────────────────


def test_should_log_true_for_hold_action():
    assert _should_log(_analyzed(action="hold")) is True


def test_should_log_true_for_hold_case_insensitive():
    assert _should_log(_analyzed(action="HOLD")) is True
    assert _should_log(_analyzed(action="  Hold  ")) is True


def test_should_log_true_for_breaking_changes_even_without_hold():
    a = _analyzed(action="apply", breaking=["removes config key X"])
    assert _should_log(a) is True


def test_should_log_false_for_clean_apply():
    a = _analyzed(action="apply", breaking=[], summary="Minor patches.")
    assert _should_log(a) is False


def test_should_log_false_when_analysis_missing():
    a = _analyzed()
    a.analysis = None
    assert _should_log(a) is False


# ─── _format_line ─────────────────────────────────────────────────────────


def test_format_line_includes_date_versions_and_token():
    line = _format_line(_analyzed(), now=FIXED_NOW)
    assert "2026-05-18" in line
    assert "11.0.13 → 11.0.14" in line
    assert "HOLD" in line
    assert "<!-- update:11.0.14 -->" in line


def test_format_line_uses_breaking_label_when_action_not_hold():
    line = _format_line(
        _analyzed(action="apply", breaking=["env X renamed"]),
        now=FIXED_NOW,
    )
    assert "BREAKING" in line
    assert "HOLD" not in line


def test_format_line_truncates_long_summary():
    long = "x" * 500
    line = _format_line(_analyzed(summary=long), now=FIXED_NOW)
    # The truncated body itself caps at 137 + the "…" — total line stays bounded.
    assert "…" in line
    assert len(line) < 250


def test_format_line_falls_back_to_first_breaking_when_summary_empty():
    line = _format_line(
        _analyzed(summary="", breaking=["wipes /data on first start"]),
        now=FIXED_NOW,
    )
    assert "wipes /data on first start" in line


# ─── _replace_or_insert_block ─────────────────────────────────────────────


def test_block_inserted_when_absent():
    body = "Existing note body.\n\n- bullet\n"
    out = _replace_or_insert_block(body, "- new line <!-- update:1.0 -->", max_lines=5)
    assert SECTION_HEADER in out
    assert LOG_START in out
    assert LOG_END in out
    assert "- new line" in out


def test_block_extends_existing_with_newest_on_top():
    body = (
        "Body.\n\n"
        f"{SECTION_HEADER}\n{LOG_START}\n"
        "- old line <!-- update:1.0 -->\n"
        f"{LOG_END}\n"
    )
    out = _replace_or_insert_block(body, "- new line <!-- update:1.1 -->", max_lines=5)
    inner = out.split(LOG_START)[1].split(LOG_END)[0]
    lines = [ln for ln in inner.splitlines() if ln.strip()]
    assert lines[0].endswith("<!-- update:1.1 -->")
    assert lines[1].endswith("<!-- update:1.0 -->")


def test_block_caps_at_max_lines():
    body = (
        "Body.\n\n"
        f"{SECTION_HEADER}\n{LOG_START}\n"
        + "\n".join(f"- old {i} <!-- update:{i}.0 -->" for i in range(10))
        + f"\n{LOG_END}\n"
    )
    out = _replace_or_insert_block(body, "- new <!-- update:99.0 -->", max_lines=3)
    inner = out.split(LOG_START)[1].split(LOG_END)[0]
    lines = [ln for ln in inner.splitlines() if ln.strip()]
    assert len(lines) == 3
    assert lines[0].endswith("<!-- update:99.0 -->")


def test_block_replaces_same_version_in_place():
    # If the cron fires twice on the same day with the same target version,
    # we don't want two near-identical lines stacking up.
    body = (
        "Body.\n\n"
        f"{SECTION_HEADER}\n{LOG_START}\n"
        "- old <!-- update:1.0 -->\n"
        f"{LOG_END}\n"
    )
    out = _replace_or_insert_block(body, "- newer same version <!-- update:1.0 -->", max_lines=5)
    inner = out.split(LOG_START)[1].split(LOG_END)[0]
    lines = [ln for ln in inner.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert "newer same version" in lines[0]


# ─── _has_existing_line ───────────────────────────────────────────────────


def test_has_existing_line_matches_token():
    body = "- foo <!-- update:1.2.3 -->\n- bar <!-- update:2.0 -->\n"
    assert _has_existing_line(body, "1.2.3") is True
    assert _has_existing_line(body, "2.0") is True


def test_has_existing_line_false_when_absent():
    body = "- foo <!-- update:1.2.3 -->\n"
    assert _has_existing_line(body, "9.9.9") is False


# ─── append_update_to_note — end to end ───────────────────────────────────


def _seed_note(tmp_path: Path, name: str = "tintes") -> Path:
    notes = tmp_path / "notes"
    notes.mkdir()
    p = notes / f"{name}.md"
    p.write_text(
        "tintes does X.\n\n- bullet\n\n<!-- curator: tintes@abc123def456 -->\n",
        encoding="utf-8",
    )
    return notes


def test_append_to_curated_note(tmp_path):
    notes = _seed_note(tmp_path)
    written = append_update_to_note(notes, _analyzed(), now=FIXED_NOW)
    assert written is not None
    text = written.read_text()
    assert SECTION_HEADER in text
    assert "11.0.13 → 11.0.14" in text
    # Curator footer must still be at the bottom.
    assert text.rstrip().endswith("<!-- curator: tintes@abc123def456 -->")


def test_append_idempotent_for_same_version(tmp_path):
    notes = _seed_note(tmp_path)
    a = _analyzed()
    first = append_update_to_note(notes, a, now=FIXED_NOW)
    second = append_update_to_note(notes, a, now=FIXED_NOW)
    assert first is not None
    assert second is None  # second call is a no-op
    inner = first.read_text().split(LOG_START)[1].split(LOG_END)[0]
    lines = [ln for ln in inner.splitlines() if ln.strip()]
    assert len(lines) == 1


def test_skip_when_not_risky(tmp_path):
    notes = _seed_note(tmp_path)
    safe = _analyzed(action="apply", breaking=[], summary="Routine patches.")
    assert append_update_to_note(notes, safe, now=FIXED_NOW) is None


def test_skip_when_file_missing(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    # No tintes.md file at all.
    assert append_update_to_note(notes, _analyzed(), now=FIXED_NOW) is None


def test_skip_manual_note_without_footer(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    p = notes / "tintes.md"
    p.write_text("manual content, no curator footer\n", encoding="utf-8")
    assert append_update_to_note(notes, _analyzed(), now=FIXED_NOW) is None
    # File must be unchanged.
    assert p.read_text() == "manual content, no curator footer\n"


def test_skip_when_notes_dir_missing(tmp_path):
    # notes_dir doesn't exist on disk yet.
    assert append_update_to_note(tmp_path / "does-not-exist", _analyzed(), now=FIXED_NOW) is None


def test_append_multiple_versions_accumulates(tmp_path):
    notes = _seed_note(tmp_path)
    append_update_to_note(notes, _analyzed(new="11.0.14"), now=FIXED_NOW)
    append_update_to_note(notes, _analyzed(new="11.0.15"), now=FIXED_NOW)
    text = (notes / "tintes.md").read_text()
    inner = text.split(LOG_START)[1].split(LOG_END)[0]
    lines = [ln for ln in inner.splitlines() if ln.strip()]
    assert len(lines) == 2
    # Newest first.
    assert lines[0].endswith("<!-- update:11.0.15 -->")
    assert lines[1].endswith("<!-- update:11.0.14 -->")
