"""Model-validity eval harness for the agent-routing layer.

Two layers, by design:

  - **Offline (CI).** `scenarios` + `scorer` are pure: feed a recorded model
    output through `parse_route` + `score_decision` and assert the verdict.
    Runs in GitHub Actions with no network and no tokens spent.
  - **Live (on-demand, local).** `runner` calls real endpoints (a local
    llama-server, free cloud tiers) to measure which models actually route
    reliably. Driven by `homelabsage eval-routing`; never runs in CI because
    the local server is LAN-only and cloud calls cost tokens/quota.

The thing under test — `homelabsage.router` — is the brain of the future
conversational interface, so this harness is not throwaway: it's how we pick
the model that powers the chat.
"""

from __future__ import annotations

from .scenarios import SCENARIOS, Expectation, Scenario
from .scorer import ModelReport, ScenarioScore, aggregate, score_decision

__all__ = [
    "SCENARIOS",
    "Scenario",
    "Expectation",
    "ScenarioScore",
    "ModelReport",
    "score_decision",
    "aggregate",
]
