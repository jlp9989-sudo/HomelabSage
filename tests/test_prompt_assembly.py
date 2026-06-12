"""Conditional prompt assembly — only ship rules whose context is present."""

from __future__ import annotations

from homelabsage.llm import build_prompt
from homelabsage.models import Update
from homelabsage.prompts import assemble, load_template


def _rule_count(text: str) -> int:
    return text.count("- If the context block")


# ─── assemble() ───────────────────────────────────────────────────


def test_assemble_keeps_only_present_keys():
    tpl = load_template("analyzer")
    out = assemble(tpl, {"cve", "pin_violation"})
    assert "If the context block contains \"cve\"" in out
    assert "If the context block contains \"pin_violation\"" in out
    # Rules whose key is absent are dropped.
    assert "image_fit" not in out
    assert "renovate" not in out
    assert _rule_count(out) == 2


def test_assemble_empty_context_drops_all_conditional_rules():
    out = assemble(load_template("analyzer"), set())
    assert _rule_count(out) == 0
    # The dangling "the following rules apply…" lead-in is removed too.
    assert "rules apply only because" not in out
    # Unconditional rules + tail survive.
    assert "Output ONLY the JSON object" in out
    assert "# Release notes" in out
    assert "{release_notes}" in out


def test_assemble_instability_rule_fires_on_any_of_three_keys():
    tpl = load_template("analyzer")
    for key in ("restart_freq", "oom_killed", "healthcheck_stale"):
        out = assemble(tpl, {key})
        assert "already unstable" in out.lower(), f"{key} should trigger the rule"


def test_assemble_markers_never_leak_into_output():
    out = assemble(load_template("analyzer"), {"cve"})
    assert "<!-- when:" not in out
    assert "BEGIN CONDITIONAL RULES" not in out
    assert "END CONDITIONAL RULES" not in out


def test_assemble_returns_template_unchanged_without_markers():
    plain = "no markers here {context}"
    assert assemble(plain, {"cve"}) == plain


# ─── build_prompt() integration ───────────────────────────────────


def test_build_prompt_includes_matching_rule_only():
    u = Update(
        source="docker", subject="immich",
        current_version="1.0", new_version="2.0",
        context={"image": "x", "cve": {"counts": {"critical": 1}}},
    )
    p = build_prompt(u)
    assert "If the context block contains \"cve\"" in p
    assert "pin_violation" not in p
    # Tail still formatted with the real context.
    assert '"cve"' in p
    assert "immich" in p


def test_build_prompt_no_context_has_zero_conditional_rules():
    u = Update(source="docker", subject="x", current_version="1", new_version="2")
    p = build_prompt(u)
    assert _rule_count(p) == 0
    assert "Output ONLY the JSON object" in p   # unconditional survives


def test_build_prompt_smaller_than_full_rule_set():
    """A single-signal update must yield a shorter prompt than one carrying
    every signal — proof the gating actually trims."""
    keys_all = {
        "orphan_since_days", "alternatives", "release_notes_diff",
        "image_size_growth", "cascade", "puid_pgid", "cve", "repo_health",
        "release_cadence", "renovate", "pr_changelog", "container_age",
        "pin_violation", "image_fit", "restart_freq", "backup_health",
    }
    one = Update(source="docker", subject="a", current_version="1",
                 new_version="2", context={"repo_health": {"status": "alive"}})
    many = Update(source="docker", subject="b", current_version="1",
                  new_version="2", context={k: {} for k in keys_all})
    assert _rule_count(build_prompt(one)) == 1
    assert _rule_count(build_prompt(many)) == 16
    assert len(build_prompt(one)) < len(build_prompt(many))
