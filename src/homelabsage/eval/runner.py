"""Live runner — drive each candidate model through every scenario.

This is the network layer of the eval. For one candidate it builds an
`LLMClient` + `Router`, routes all `SCENARIOS`, times each call, catches
transport/HTTP failures (a model that can't respond scores the scenario as
an error, not a crash), scores with the pure `scorer`, and rolls up to a
`ModelReport`. It never runs in CI — it talks to a local server (LAN-only)
and to metered cloud tiers.

The raw model output of every call is captured too, so a live run can dump
fixtures for the offline CI suite to replay deterministically.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..llm import LLMClient
from ..router import Router, parse_route
from .candidates import Candidate
from .scenarios import SCENARIOS, Scenario
from .scorer import ModelReport, ScenarioScore, aggregate, score_decision


@dataclass
class CallRecord:
    """Per-(model, scenario) raw capture — the seed for an offline fixture."""

    model: str
    scenario_id: str
    raw: str
    latency_ms: int | None
    error: str | None = None


@dataclass
class ModelRun:
    candidate: Candidate
    report: ModelReport
    scores: list[ScenarioScore]
    records: list[CallRecord] = field(default_factory=list)


async def run_candidate(
    candidate: Candidate,
    catalog: dict[str, dict],
    scenarios: list[Scenario] = SCENARIOS,
    *,
    on_scenario=None,
) -> ModelRun:
    """Route every scenario through one candidate model.

    `on_scenario(scenario, score)` is an optional progress callback. Transport
    errors are caught per-scenario: the model stays in the matrix with an
    error count rather than aborting the whole run.
    """
    client = LLMClient(candidate.config)
    router = Router(client, catalog)

    scores: list[ScenarioScore] = []
    records: list[CallRecord] = []
    for sc in scenarios:
        started = time.monotonic()
        raw = ""
        err: str | None = None
        try:
            decision = await router.route(sc.message)
            raw = decision.raw
            latency = int((time.monotonic() - started) * 1000)
            score = score_decision(sc, decision)
            score.latency_ms = latency
        except Exception as e:  # noqa: BLE001 — any transport/HTTP failure
            latency = int((time.monotonic() - started) * 1000)
            err = f"{type(e).__name__}: {e}"
            # Build a failed score so aggregate counts the scenario.
            score = ScenarioScore(
                scenario_id=sc.id,
                valid_json=False,
                tool_correct=False,
                hallucinated=False,
                confirmation_correct=False,
                args_correct=None,
                chaining_correct=None,
                latency_ms=None,
                error=err,
            )
        scores.append(score)
        records.append(CallRecord(
            model=candidate.label, scenario_id=sc.id,
            raw=raw, latency_ms=score.latency_ms, error=err,
        ))
        if on_scenario is not None:
            on_scenario(sc, score)

    report = aggregate(candidate.label, scores)
    return ModelRun(candidate=candidate, report=report, scores=scores, records=records)


def score_records(
    records: list[CallRecord],
    catalog: dict[str, dict],
    scenarios: list[Scenario] = SCENARIOS,
) -> dict[str, ModelReport]:
    """Re-score recorded raw outputs WITHOUT any network — the offline path.

    Replays fixtures through `parse_route` + `score_decision` + `aggregate`,
    grouped by model. This is what the CI suite calls so the published
    metrics are reproducible from the committed fixtures alone.
    """
    known = set(catalog)
    by_id = {s.id: s for s in scenarios}
    by_model: dict[str, list[ScenarioScore]] = {}
    for rec in records:
        sc = by_id.get(rec.scenario_id)
        if sc is None:
            continue
        if rec.error is not None:
            score = ScenarioScore(
                scenario_id=sc.id, valid_json=False, tool_correct=False,
                hallucinated=False, confirmation_correct=False,
                args_correct=None, chaining_correct=None,
                latency_ms=None, error=rec.error,
            )
        else:
            decision = parse_route(rec.raw, known_tools=known)
            score = score_decision(sc, decision)
            score.latency_ms = rec.latency_ms
        by_model.setdefault(rec.model, []).append(score)
    return {model: aggregate(model, scores) for model, scores in by_model.items()}
