"""Tests for image-pin enforcement."""

from __future__ import annotations

from homelabsage.image_pins import PinVerdict, _matches_pin, evaluate

# ─── per-spec matcher ────────────────────────────────────────────────


def test_matches_pin_glob_in_range():
    ok, _ = _matches_pin("8.5.2", "8.*")
    assert ok


def test_matches_pin_glob_outside_range():
    ok, reason = _matches_pin("9.0.0", "8.*")
    assert not ok
    assert "does not match" in reason


def test_matches_pin_exact():
    ok, _ = _matches_pin("1.2.3", "1.2.3")
    assert ok


def test_matches_pin_exact_mismatch():
    ok, reason = _matches_pin("1.2.4", "1.2.3")
    assert not ok
    assert "differs" in reason


def test_matches_pin_lte_operator():
    ok, _ = _matches_pin("2026.5", "<=2026.5")
    assert ok
    bad, _ = _matches_pin("2026.6", "<=2026.5")
    assert not bad


def test_matches_pin_lt_operator():
    bad, _ = _matches_pin("2026.5", "<2026.5")
    assert not bad
    ok, _ = _matches_pin("2026.4", "<2026.5")
    assert ok


def test_matches_pin_gte_operator():
    ok, _ = _matches_pin("2026.6", ">=2026.5")
    assert ok
    bad, _ = _matches_pin("2026.4", ">=2026.5")
    assert not bad


def test_matches_pin_strips_v_prefix():
    ok, _ = _matches_pin("v8.5", "8.*")
    assert ok


# ─── evaluate ────────────────────────────────────────────────────────


def test_evaluate_no_match_returns_none():
    out = evaluate(
        subject="unmatched-service",
        new_version="2.0",
        pins={"mealie": "1.*"},
    )
    assert out is None


def test_evaluate_within_pin_returns_none():
    out = evaluate(
        subject="elasticsearch",
        new_version="8.10.0",
        pins={"elasticsearch": "8.*"},
    )
    assert out is None


def test_evaluate_crosses_pin_returns_verdict():
    out = evaluate(
        subject="elasticsearch",
        new_version="9.0.0",
        pins={"elasticsearch": "8.*"},
    )
    assert isinstance(out, PinVerdict)
    assert out.pin == "8.*"
    assert out.new_version == "9.0.0"
    ctx = out.to_context()
    assert ctx["pin"] == "8.*"
    assert ctx["pin_subject"] == "elasticsearch"


def test_evaluate_case_insensitive_subject():
    out = evaluate(
        subject="Elasticsearch",
        new_version="9.0.0",
        pins={"ELASTICSEARCH": "8.*"},
    )
    assert out is not None


def test_evaluate_glob_subject_key():
    """Pin key can itself be a glob — `homeassistant/*` matches the repo."""
    out = evaluate(
        subject="homeassistant/core",
        new_version="2026.7",
        pins={"homeassistant/*": "<=2026.5"},
    )
    assert out is not None
    assert out.pin_subject == "homeassistant/*"


def test_evaluate_operator_pin_blocks_upgrade():
    out = evaluate(
        subject="homeassistant/core",
        new_version="2026.7",
        pins={"homeassistant/core": "<=2026.5"},
    )
    assert out is not None


def test_evaluate_operator_pin_allows_within():
    out = evaluate(
        subject="homeassistant/core",
        new_version="2026.4",
        pins={"homeassistant/core": "<=2026.5"},
    )
    assert out is None


def test_evaluate_first_match_wins():
    out = evaluate(
        subject="mealie",
        new_version="2.0",
        pins={
            "mealie": "1.*",
            "*": "0.*",  # would also match but the explicit key was first
        },
    )
    assert out is not None
    assert out.pin_subject == "mealie"
