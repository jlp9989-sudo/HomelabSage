"""Pushover output — direct push to the user's Pushover account.

Apprise speaks Pushover, but a dedicated output gives the user:
  - Per-severity `priority` (-2 silent → 2 emergency) mapped from
    HomelabSage severity
  - First-class settings UI surface alongside Telegram/Discord
  - No 3rd-party dep — Pushover's API is one URL + form params

Tokens:
  - **app token** (`token`): identifies the app sending the message
  - **user key** (`user_key`): identifies the recipient(s)
Both are required. Sent in the POST form body, never in the URL.

`https://api.pushover.net/1/messages.json`
"""

from __future__ import annotations

import logging

import httpx

from ..models import AnalyzedUpdate, Severity
from . import Output

log = logging.getLogger(__name__)


_PUSHOVER_URL = "https://api.pushover.net/1/messages.json"

# Pushover priority scale: -2 (silent, no notification), -1 (quiet),
# 0 (default), 1 (high), 2 (emergency — repeats until acknowledged).
# We deliberately CAP at 1 by default to avoid silently triggering
# emergency-grade repeats. Users opt in by overriding the bypass-
# severity field in the config block.
_SEVERITY_PRIORITY: dict[str, int] = {
    "critical": 1,
    "high":     1,
    "medium":   0,
    "info":    -1,
}


class PushoverOutput(Output):
    id = "pushover"
    is_push = True

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._min = Severity(cfg.min_severity)

    def _should_send(self, item: AnalyzedUpdate) -> bool:
        if not self.cfg.enabled or not self.cfg.token or not self.cfg.user_key:
            return False
        return self._severity_passes(item)

    def _build_form(self, item: AnalyzedUpdate) -> dict[str, str]:
        u = item.update
        a = item.analysis
        assert a is not None
        title = f"{u.subject}  {u.current_version} → {u.new_version}"
        body_parts = [a.summary or "(no summary)"]
        if a.breaking_changes:
            body_parts.append("")
            body_parts.append("Breaking:")
            body_parts.extend(f"• {b}" for b in a.breaking_changes[:5])
        if a.recommended_action:
            body_parts.append("")
            body_parts.append(f"Action: {a.recommended_action}")
        body = "\n".join(body_parts)
        priority = self.cfg.emergency_at_critical and a.severity == Severity.CRITICAL
        prio_value = 2 if priority else _SEVERITY_PRIORITY.get(
            a.severity.value, 0,
        )
        form: dict[str, str] = {
            "token": self.cfg.token,
            "user": self.cfg.user_key,
            "title": title[:250],     # Pushover title cap
            "message": body[:1024],   # Pushover body cap
            "priority": str(prio_value),
        }
        if u.release_url:
            form["url"] = u.release_url
            form["url_title"] = "Release notes"
        if self.cfg.device:
            form["device"] = self.cfg.device
        if prio_value == 2:
            # Emergency requires retry + expire; pick conservative
            # defaults: re-deliver every 60 s, give up after 1 h.
            form["retry"] = "60"
            form["expire"] = "3600"
        return form

    async def send(self, item: AnalyzedUpdate) -> bool:
        if not self._should_send(item):
            return True   # nothing to do — don't retry
        # Pushover wants form-encoded `data=`, not JSON — keep custom POST
        # instead of `_push_json`. Failure path shares the base log helper.
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(_PUSHOVER_URL, data=self._build_form(item))
                r.raise_for_status()
        except httpx.HTTPError as e:
            self._log_push_failure(item, e)
            return False
        return True
