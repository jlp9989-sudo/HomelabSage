"""Pure scoring for routing scenarios — no I/O, no network.

`score_decision` compares one `RouteDecision` against a `Scenario`'s
expectation; `aggregate` rolls per-scenario scores into a `ModelReport`
(the row that lands in the committed matrix). Keeping this side-effect-free
is what lets the offline CI suite replay recorded fixtures and assert the
metrics deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..router import RouteDecision
from .scenarios import Expectation, Scenario


@dataclass
class ScenarioScore:
    scenario_id: str
    valid_json: bool
    tool_correct: bool
    hallucinated: bool
    confirmation_correct: bool
    args_correct: bool | None       # None = scenario carries no arg expectation
    chaining_correct: bool | None   # None = not a multi-step scenario
    latency_ms: int | None = None
    error: str | None = None        # transport/HTTP failure → model unreachable

    @property
    def passed(self) -> bool:
        """Overall pass: every applicable check green and nothing hallucinated."""
        if self.error is not None:
            return False
        return (
            self.valid_json
            and self.tool_correct
            and not self.hallucinated
            and self.confirmation_correct
            and self.args_correct is not False
            and self.chaining_correct is not False
        )


def _arg_matches(actual: dict, expected: dict) -> bool:
    """Lenient per-tool argument check. `...` = key present (any value);
    strings = case-insensitive substring; everything else = equality."""
    for key, want in expected.items():
        if key not in actual:
            return False
        got = actual[key]
        if want is ...:
            continue
        if isinstance(want, str):
            if str(want).lower() not in str(got).lower():
                return False
        elif got != want:
            return False
    return True


def score_decision(scenario: Scenario, decision: RouteDecision) -> ScenarioScore:
    exp: Expectation = scenario.expectation
    names = decision.tool_names
    hallucinated = bool(decision.hallucinated_tools)

    if exp.is_no_tool:
        # Correct routing is to call nothing. Any tool call is wrong; a
        # hallucinated name is the worst case (tracked separately too).
        tool_correct = len(names) == 0
        confirmation_correct = decision.needs_confirmation is False
        return ScenarioScore(
            scenario_id=scenario.id,
            valid_json=decision.valid_json,
            tool_correct=tool_correct,
            hallucinated=hallucinated,
            confirmation_correct=confirmation_correct,
            args_correct=None,
            chaining_correct=None,
        )

    expected = exp.expected_tools
    present = all(t in names for t in expected)
    extra = [t for t in names if t not in expected]
    tool_correct = present and (exp.allow_extra_tools or not extra)

    # Chaining: expected tools appear in the required relative order.
    chaining_correct: bool | None = None
    if exp.require_order:
        first_idx = []
        ok = True
        for t in expected:
            try:
                first_idx.append(names.index(t))
            except ValueError:
                ok = False
                break
        chaining_correct = ok and first_idx == sorted(first_idx)

    # Arguments: per-tool required subset on the first matching call.
    args_correct: bool | None = None
    if exp.expected_args:
        args_correct = True
        for tool, want in exp.expected_args.items():
            call = next((c for c in decision.tool_calls if c.tool == tool), None)
            if call is None or not call.arguments_ok or not _arg_matches(call.arguments, want):
                args_correct = False
                break

    confirmation_correct = decision.needs_confirmation == exp.needs_confirmation

    return ScenarioScore(
        scenario_id=scenario.id,
        valid_json=decision.valid_json,
        tool_correct=tool_correct,
        hallucinated=hallucinated,
        confirmation_correct=confirmation_correct,
        args_correct=args_correct,
        chaining_correct=chaining_correct,
    )


def _pct(num: int, den: int) -> float:
    return round(100.0 * num / den, 1) if den else 0.0


def _percentile(values: list[int], q: float) -> int | None:
    """Nearest-rank percentile (q in [0,1]). None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    # nearest-rank: ceil(q*N) index, 1-based → clamp into range
    import math
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


@dataclass
class ModelReport:
    model: str
    n: int
    n_errors: int
    pct_valid_json: float
    pct_tool_correct: float
    pct_args_correct: float
    pct_chaining_correct: float
    pct_hallucinated: float          # lower is better
    pct_confirmation_correct: float
    pct_passed: float
    latency_p50_ms: int | None
    latency_p95_ms: int | None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def aggregate(model: str, scores: list[ScenarioScore]) -> ModelReport:
    """Roll per-scenario scores into one matrix row.

    Percentage metrics are over ALL scenarios (an errored call counts as a
    fail for that scenario — a model that couldn't respond didn't route),
    except `args`/`chaining` which average only over scenarios that carry
    that expectation. Latency percentiles use only successful calls.
    """
    n = len(scores)
    latencies = [s.latency_ms for s in scores if s.error is None and s.latency_ms is not None]
    args_applicable = [s for s in scores if s.args_correct is not None and s.error is None]
    chain_applicable = [s for s in scores if s.chaining_correct is not None and s.error is None]
    return ModelReport(
        model=model,
        n=n,
        n_errors=sum(1 for s in scores if s.error is not None),
        pct_valid_json=_pct(sum(1 for s in scores if s.valid_json), n),
        pct_tool_correct=_pct(sum(1 for s in scores if s.tool_correct), n),
        pct_args_correct=_pct(
            sum(1 for s in args_applicable if s.args_correct), len(args_applicable),
        ),
        pct_chaining_correct=_pct(
            sum(1 for s in chain_applicable if s.chaining_correct), len(chain_applicable),
        ),
        pct_hallucinated=_pct(sum(1 for s in scores if s.hallucinated), n),
        pct_confirmation_correct=_pct(
            sum(1 for s in scores if s.confirmation_correct), n,
        ),
        pct_passed=_pct(sum(1 for s in scores if s.passed), n),
        latency_p50_ms=_percentile(latencies, 0.50),
        latency_p95_ms=_percentile(latencies, 0.95),
    )
