"""Offline CI layer for the agent-routing eval.

No network, no tokens: everything here exercises the pure routing pipeline
(`router.parse_route` + `eval.scorer` + `eval.runner.score_records`) against
recorded fixtures, so the published model-validity matrix is reproducible
from `tests/fixtures/routing/recorded.json` alone. The LIVE measurement
(real local / cloud endpoints) is `homelabsage eval-routing`, never run in CI.
"""

from __future__ import annotations

import json
from pathlib import Path

from homelabsage import mcp_tools
from homelabsage.eval import SCENARIOS, score_decision
from homelabsage.eval.runner import CallRecord, score_records
from homelabsage.router import (
    DESTRUCTIVE_TOOLS,
    ROUTING_SCHEMA,
    parse_route,
)

FIXTURE = Path(__file__).parent / "fixtures" / "routing" / "recorded.json"
CATALOG = mcp_tools.TOOLS
KNOWN = set(CATALOG)


# --------------------------------------------------------------------------
# Catalog invariants — a rename mustn't silently make a write tool read-only.
# --------------------------------------------------------------------------

def test_destructive_tools_all_exist_in_catalog():
    missing = sorted(t for t in DESTRUCTIVE_TOOLS if t not in CATALOG)
    assert missing == [], f"DESTRUCTIVE_TOOLS not in live catalog: {missing}"


def test_routing_schema_is_strict_compatible():
    # Flat strict object: closed, every property required.
    assert ROUTING_SCHEMA["additionalProperties"] is False
    props = set(ROUTING_SCHEMA["properties"])
    assert set(ROUTING_SCHEMA["required"]) == props
    item = ROUTING_SCHEMA["properties"]["tool_calls"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == set(item["properties"])


def test_scenarios_reference_real_tools_only():
    for sc in SCENARIOS:
        for tool in sc.expectation.expected_tools:
            assert tool in CATALOG, f"{sc.id} expects unknown tool {tool!r}"


# --------------------------------------------------------------------------
# parse_route tolerance
# --------------------------------------------------------------------------

def test_parse_route_plain_object():
    raw = json.dumps({
        "reasoning": "r", "needs_confirmation": False,
        "tool_calls": [{"tool": "audit", "arguments_json": "{}"}],
        "direct_answer": None,
    })
    d = parse_route(raw, known_tools=KNOWN)
    assert d.valid_json
    assert d.tool_names == ["audit"]
    assert d.tool_calls[0].arguments_ok
    assert d.tool_calls[0].known


def test_parse_route_strips_code_fence():
    inner = json.dumps({
        "reasoning": "r", "needs_confirmation": False,
        "tool_calls": [{"tool": "audit", "arguments_json": "{}"}],
        "direct_answer": None,
    })
    d = parse_route(f"```json\n{inner}\n```", known_tools=KNOWN)
    assert d.valid_json and d.tool_names == ["audit"]


def test_parse_route_accepts_nested_object_arguments():
    # Some models emit arguments as a nested object despite the string schema.
    raw = json.dumps({
        "reasoning": "r", "needs_confirmation": False,
        "tool_calls": [{"tool": "where_is", "arguments_json": {"name": "immich"}}],
        "direct_answer": None,
    })
    d = parse_route(raw, known_tools=KNOWN)
    assert d.tool_calls[0].arguments_ok
    assert d.tool_calls[0].arguments == {"name": "immich"}


def test_parse_route_flags_hallucinated_tool():
    raw = json.dumps({
        "reasoning": "r", "needs_confirmation": False,
        "tool_calls": [{"tool": "restart_container", "arguments_json": "{}"}],
        "direct_answer": None,
    })
    d = parse_route(raw, known_tools=KNOWN)
    assert d.hallucinated_tools == ["restart_container"]
    assert not d.tool_calls[0].known


def test_parse_route_non_json_is_invalid():
    d = parse_route("lo siento, no puedo hacer eso", known_tools=KNOWN)
    assert d.valid_json is False
    assert d.tool_calls == []


# --------------------------------------------------------------------------
# score_decision dimensions
# --------------------------------------------------------------------------

def _scenario(scenario_id: str):
    return next(s for s in SCENARIOS if s.id == scenario_id)


def test_score_single_tool_and_args():
    sc = _scenario("status_applied")
    d = parse_route(json.dumps({
        "reasoning": "r", "needs_confirmation": False,
        "tool_calls": [{"tool": "list_updates", "arguments_json": json.dumps({"status": "applied"})}],
        "direct_answer": None,
    }), known_tools=KNOWN)
    score = score_decision(sc, d)
    assert score.passed and score.tool_correct and score.args_correct


def test_score_wrong_arg_fails_args_only():
    sc = _scenario("where_is_jellyfin")
    d = parse_route(json.dumps({
        "reasoning": "r", "needs_confirmation": False,
        "tool_calls": [{"tool": "where_is", "arguments_json": json.dumps({"name": "plex"})}],
        "direct_answer": None,
    }), known_tools=KNOWN)
    score = score_decision(sc, d)
    assert score.tool_correct is True
    assert score.args_correct is False
    assert score.passed is False


def test_score_chaining_requires_order():
    sc = _scenario("search_then_explain")
    # right tools, wrong order
    d = parse_route(json.dumps({
        "reasoning": "r", "needs_confirmation": False,
        "tool_calls": [
            {"tool": "explain", "arguments_json": json.dumps({"update_id": "x"})},
            {"tool": "search_updates", "arguments_json": json.dumps({"q": "immich"})},
        ],
        "direct_answer": None,
    }), known_tools=KNOWN)
    score = score_decision(sc, d)
    assert score.chaining_correct is False


def test_score_confirmation_gate_for_write_tool():
    sc = _scenario("purge_old")
    missed = parse_route(json.dumps({
        "reasoning": "r", "needs_confirmation": False,
        "tool_calls": [{"tool": "purge_old_updates", "arguments_json": json.dumps({"older_than_days": 180})}],
        "direct_answer": None,
    }), known_tools=KNOWN)
    assert score_decision(sc, missed).confirmation_correct is False


def test_score_no_tool_scenario_rejects_any_call():
    sc = _scenario("restart_no_tool")
    # correct: no tool
    none = parse_route(json.dumps({
        "reasoning": "r", "needs_confirmation": False,
        "tool_calls": [], "direct_answer": "No puedo reiniciar contenedores.",
    }), known_tools=KNOWN)
    assert score_decision(sc, none).tool_correct is True
    # wrong: hallucinated tool
    bad = parse_route(json.dumps({
        "reasoning": "r", "needs_confirmation": False,
        "tool_calls": [{"tool": "restart_container", "arguments_json": "{}"}],
        "direct_answer": None,
    }), known_tools=KNOWN)
    s = score_decision(sc, bad)
    assert s.tool_correct is False and s.hallucinated is True


# --------------------------------------------------------------------------
# Deterministic replay of the committed fixtures → exact matrix numbers
# --------------------------------------------------------------------------

def _load_fixture_reports():
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    records = [CallRecord(**r) for r in doc["records"]]
    return score_records(records, CATALOG)


def test_fixture_good_model_perfect():
    r = _load_fixture_reports()["good-model"]
    assert r.n == len(SCENARIOS)
    assert r.n_errors == 0
    assert r.pct_passed == 100.0
    assert r.pct_valid_json == 100.0
    assert r.pct_hallucinated == 0.0


def test_fixture_weak_model_exact_breakdown():
    r = _load_fixture_reports()["weak-model"]
    # Locked-in numbers: a regression in any scoring dimension trips here.
    assert r.n_errors == 1
    assert r.pct_valid_json == 92.9
    assert r.pct_tool_correct == 85.7
    assert r.pct_args_correct == 87.5
    assert r.pct_chaining_correct == 50.0
    assert r.pct_hallucinated == 7.1
    assert r.pct_confirmation_correct == 85.7
    assert r.pct_passed == 64.3


def test_fixture_covers_every_scenario_per_model():
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    ids = {s.id for s in SCENARIOS}
    for model in ("good-model", "weak-model"):
        got = {r["scenario_id"] for r in doc["records"] if r["model"] == model}
        assert got == ids, f"{model} missing {ids - got}"
