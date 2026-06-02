"""Discord output — push a per-update embed to a channel via webhook.

Discord webhooks are the lowest-friction notification path on r/selfhosted:
the user creates a channel webhook in the Discord UI (Server Settings →
Integrations → Webhooks → New Webhook → Copy URL) and pastes the URL here.
No app registration, no bot, no OAuth.

Format:
  - One embed per update with a severity-coloured stripe.
  - Title is "<subject> <current> → <new>" (matches Telegram for parity).
  - First field shows the LLM summary, additional fields surface
    breaking changes and recommended action when present.
  - `release_url` is wired to the embed `url` so the title is clickable.

The webhook URL is a secret (anyone with it can post to the channel) so it
goes into `.env` / the settings UI password slot, never into the deploy
default `config.yaml`.
"""

from __future__ import annotations

import logging

import httpx

from ..config import DiscordOutputConfig
from ..models import AnalyzedUpdate, Severity
from . import Output

log = logging.getLogger(__name__)


# Discord embeds take a decimal integer for the colour stripe; we keep the
# mapping tight (red / orange / yellow / blue) so the channel scans like
# Telegram's emoji bar. The hex literals match the most common
# severity-palette across Grafana / PagerDuty / Sentry.
_SEVERITY_COLOR: dict[str, int] = {
    "critical": 0xC0392B,  # red
    "high": 0xE67E22,      # orange
    "medium": 0xF1C40F,    # yellow
    "info": 0x3498DB,      # blue
}


class DiscordOutput(Output):
    id = "discord"
    is_push = True

    def __init__(self, cfg: DiscordOutputConfig):
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
        assert a is not None  # checked in _should_send
        title = f"{u.subject}  {u.current_version} → {u.new_version}"
        fields: list[dict] = [
            {
                "name": f"{a.severity.value.upper()} — summary",
                "value": (a.summary or "")[:1024] or "(no summary)",
                "inline": False,
            }
        ]
        if a.breaking_changes:
            fields.append({
                "name": "Breaking",
                "value": "\n".join(f"• {b}" for b in a.breaking_changes[:5])[:1024],
                "inline": False,
            })
        if a.recommended_action:
            fields.append({
                "name": "Recommended action",
                "value": a.recommended_action[:1024],
                "inline": False,
            })
        embed: dict = {
            "title": title[:256],
            "color": _SEVERITY_COLOR.get(a.severity.value, _SEVERITY_COLOR["info"]),
            "fields": fields,
            "footer": {"text": f"HomelabSage · {u.source}"},
        }
        if u.release_url:
            embed["url"] = u.release_url
        payload: dict = {"embeds": [embed]}
        if self.cfg.username:
            payload["username"] = self.cfg.username
        if self.cfg.avatar_url:
            payload["avatar_url"] = self.cfg.avatar_url
        return payload

    async def send(self, item: AnalyzedUpdate) -> None:
        if not self._should_send(item):
            return
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(self.cfg.webhook_url, json=self._build_payload(item))
                r.raise_for_status()
        except httpx.HTTPError as e:
            log.error("Discord push failed for %s: %s", item.id, e)
