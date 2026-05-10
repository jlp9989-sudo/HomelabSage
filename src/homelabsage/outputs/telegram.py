"""Telegram output — push notification if severity ≥ min_severity."""

from __future__ import annotations

import logging

import httpx

from ..config import TelegramOutputConfig
from ..models import AnalyzedUpdate, Severity
from . import Output

log = logging.getLogger(__name__)


class TelegramOutput(Output):
    id = "telegram"

    def __init__(self, cfg: TelegramOutputConfig):
        self.cfg = cfg
        self._min = Severity(cfg.min_severity)

    def _should_send(self, item: AnalyzedUpdate) -> bool:
        if not self.cfg.enabled or not self.cfg.bot_token or not self.cfg.chat_id:
            return False
        if not item.analysis:
            return False
        return item.analysis.severity.order >= self._min.order

    def _format(self, item: AnalyzedUpdate) -> str:
        u = item.update
        a = item.analysis
        assert a is not None  # checked in _should_send
        head = f"*{u.subject}*  `{u.current_version}` → `{u.new_version}`"
        sev_emoji = {"critical": "🔴", "high": "🟠", "medium": "🟡", "info": "🔵"}[a.severity.value]
        lines = [head, f"{sev_emoji} {a.severity.value.upper()} — {a.summary}"]
        if a.breaking_changes:
            lines.append("*Breaking:* " + "; ".join(a.breaking_changes[:3]))
        if a.recommended_action:
            lines.append(f"*Acción:* {a.recommended_action}")
        if u.release_url:
            lines.append(f"[Release notes]({u.release_url})")
        return "\n".join(lines)

    async def send(self, item: AnalyzedUpdate) -> None:
        if not self._should_send(item):
            return
        url = f"https://api.telegram.org/bot{self.cfg.bot_token}/sendMessage"
        payload = {
            "chat_id": self.cfg.chat_id,
            "text": self._format(item),
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(url, json=payload)
                r.raise_for_status()
        except httpx.HTTPError as e:
            log.error("Telegram push failed for %s: %s", item.id, e)
