"""Slack output — incoming-webhook with Block Kit message.

Apprise covers Slack already through its URL syntax, but a dedicated
output gives the user:
  - Block Kit formatting that renders properly in the Slack client
    (headers + sections + context blocks + colored attachment stripe)
  - No 3rd-party-library dep; just `httpx` like every other output
  - First-class settings UI surface alongside Telegram/Discord

Slack incoming webhooks accept both legacy `attachments` AND modern
`blocks`. We use both: `blocks` is the message body, `attachments`
carries the severity-coloured stripe (legacy Slack still shows it
on the side, modern Slack treats it as a fallback).

URL format:
    https://hooks.slack.com/services/T<TEAM>/B<BOT>/<SECRET>

Anyone with the URL can post to the channel — treat it as a secret.
"""

from __future__ import annotations

import logging

import httpx

from ..models import AnalyzedUpdate, Severity
from . import Output
from ._errlog import safe_error

log = logging.getLogger(__name__)


# Slack accepts hex without `#` in attachment color, but the docs use
# `#`-prefixed strings — we match the official examples.
_SEVERITY_COLOR: dict[str, str] = {
    "critical": "#c0392b",
    "high":     "#e67e22",
    "medium":   "#a48618",
    "info":     "#205b8a",
}


class SlackOutput(Output):
    id = "slack"
    is_push = True

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._min = Severity(cfg.min_severity)

    def _should_send(self, item: AnalyzedUpdate) -> bool:
        if not self.cfg.enabled or not self.cfg.webhook_url:
            return False
        if not item.analysis:
            return False
        return item.analysis.severity.order >= self._min.order

    def _build_payload(self, item: AnalyzedUpdate) -> dict:
        u = item.update
        a = item.analysis
        assert a is not None
        title = f"{u.subject}  {u.current_version} → {u.new_version}"
        # Body composed of one section per logical chunk so Slack
        # renders nice spacing.
        blocks: list[dict] = [
            {"type": "header", "text": {"type": "plain_text",
                                         "text": title[:150]}},
            {"type": "section",
             "text": {"type": "mrkdwn",
                      "text": f"*Severity:* `{a.severity.value}`\n"
                              f"*Summary:* {a.summary or '_(no summary)_'}"}},
        ]
        if a.breaking_changes:
            bullets = "\n".join(f"• {b}" for b in a.breaking_changes[:6])
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn",
                         "text": f"*Breaking changes:*\n{bullets}"},
            })
        if a.recommended_action:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn",
                         "text": f"*Recommended action:*\n{a.recommended_action}"},
            })
        ctx_elems: list[dict] = []
        if u.release_url:
            ctx_elems.append({
                "type": "mrkdwn", "text": f"<{u.release_url}|Release notes>",
            })
        ctx_elems.append({"type": "mrkdwn", "text": f"id: `{item.id}`"})
        blocks.append({"type": "context", "elements": ctx_elems})

        return {
            # `text` is required as the fallback for old Slack clients
            # and for push notifications on mobile.
            "text": title,
            "username": self.cfg.username or "HomelabSage",
            "blocks": blocks,
            "attachments": [
                {
                    "color": _SEVERITY_COLOR.get(a.severity.value, "#205b8a"),
                    "fallback": title,
                },
            ],
        }

    async def send(self, item: AnalyzedUpdate) -> None:
        if not self._should_send(item):
            return
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    self.cfg.webhook_url, json=self._build_payload(item),
                )
                r.raise_for_status()
        except httpx.HTTPError as e:
            log.error("Slack push failed for %s: %s", item.id, safe_error(e))
