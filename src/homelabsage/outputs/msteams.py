"""Microsoft Teams output — incoming webhook via Adaptive Card.

Teams used to accept the simpler MessageCard format; modern Teams
expects Adaptive Cards 1.5+ inside an `attachments[]` envelope. We
emit the Adaptive Card form because the legacy MessageCard channel
is being deprecated through 2026.

URL format (Power Automate workflow URL):
    https://prod-XX.westeurope.logic.azure.com/workflows/.../triggers/manual/paths/invoke?...

The legacy `outlook.office.com/webhook/...` URLs still work for now
and accept the same payload shape.
"""

from __future__ import annotations

import logging

from ..models import AnalyzedUpdate, Severity
from . import Output

log = logging.getLogger(__name__)


# Adaptive Cards have a small set of named accent colors. Map severity
# to the closest one — Teams renders these as a left-edge stripe.
_SEVERITY_COLOR: dict[str, str] = {
    "critical": "attention",   # red
    "high":     "warning",     # orange
    "medium":   "accent",      # blue
    "info":     "good",        # green
}


def _build_adaptive_card(item: AnalyzedUpdate) -> dict:
    u = item.update
    a = item.analysis
    assert a is not None
    body: list[dict] = [
        {
            "type": "TextBlock",
            "size": "Large", "weight": "Bolder",
            "text": f"{u.subject}",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "spacing": "None",
            "isSubtle": True,
            "text": f"{u.current_version} → {u.new_version}",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "spacing": "Medium",
            "color": _SEVERITY_COLOR.get(a.severity.value, "default"),
            "weight": "Bolder",
            "text": f"Severity: {a.severity.value}",
        },
        {
            "type": "TextBlock",
            "text": a.summary or "(no summary)",
            "wrap": True,
        },
    ]
    if a.breaking_changes:
        body.append({
            "type": "TextBlock",
            "weight": "Bolder",
            "text": "Breaking changes",
        })
        bullets = "\n".join(f"- {b}" for b in a.breaking_changes[:6])
        body.append({
            "type": "TextBlock", "text": bullets, "wrap": True,
        })
    if a.recommended_action:
        body.append({
            "type": "TextBlock",
            "weight": "Bolder",
            "text": "Recommended action",
        })
        body.append({
            "type": "TextBlock",
            "text": a.recommended_action,
            "wrap": True,
        })

    actions: list[dict] = []
    if u.release_url:
        actions.append({
            "type": "Action.OpenUrl",
            "title": "Release notes",
            "url": u.release_url,
        })

    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": body,
    }
    if actions:
        card["actions"] = actions

    # Teams expects the card wrapped in an `attachments[]` envelope.
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": card,
            },
        ],
    }


class MSTeamsOutput(Output):
    id = "msteams"
    is_push = True

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._min = Severity(cfg.min_severity)

    def _should_send(self, item: AnalyzedUpdate) -> bool:
        if not self.cfg.enabled or not self.cfg.webhook_url:
            return False
        return self._severity_passes(item)

    async def send(self, item: AnalyzedUpdate) -> None:
        if not self._should_send(item):
            return
        await self._push_json(
            self.cfg.webhook_url, item=item, json=_build_adaptive_card(item),
        )
