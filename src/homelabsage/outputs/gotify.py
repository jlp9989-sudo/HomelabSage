"""Gotify output — push to a self-hosted Gotify server.

Gotify is a tiny self-hosted notification server popular in the homelab
crowd. Each "app" the user creates on the Gotify UI has its own token; that
token is what authenticates a POST to `/message`. There is no chat / channel
abstraction — every message lands in the app's inbox.

Wire format:
  POST {server}/message?token={token}
       {"title": "...", "message": "...", "priority": N}

Priority is a free-form 1-10 integer in Gotify's data model; we map our
severities to a Gotify-typical scale so that:
  - 8+ triggers Android high-importance (full-screen / sound)
  - 4-7 triggers default-priority banners
  - 1-3 silent bundle

Authentication is just the token; no headers required. The server URL must
include the scheme (`https://gotify.example.com`).
"""

from __future__ import annotations

import logging

from ..config import GotifyOutputConfig
from ..models import AnalyzedUpdate, Severity
from . import Output

log = logging.getLogger(__name__)


# Default mapping; can be overridden per-installation via the
# `priority_overrides` config dict if the user wants e.g. critical to be 10
# (forces full-screen on every supported Android version).
_DEFAULT_PRIORITY: dict[str, int] = {
    "critical": 8,
    "high": 6,
    "medium": 4,
    "info": 2,
}


class GotifyOutput(Output):
    id = "gotify"
    is_push = True

    def __init__(self, cfg: GotifyOutputConfig):
        self.cfg = cfg
        self._min = Severity(cfg.min_severity)

    def _should_send(self, item: AnalyzedUpdate) -> bool:
        if not self.cfg.enabled or not self.cfg.server_url or not self.cfg.token:
            return False
        return self._severity_passes(item)

    def _priority_for(self, severity: str) -> int:
        if severity in self.cfg.priority_overrides:
            return self.cfg.priority_overrides[severity]
        return _DEFAULT_PRIORITY.get(severity, _DEFAULT_PRIORITY["info"])

    def _format(self, item: AnalyzedUpdate) -> dict:
        u = item.update
        a = item.analysis
        assert a is not None  # checked in _should_send
        lines = [a.summary or "(no summary)"]
        if a.breaking_changes:
            lines.append("\nBreaking:")
            lines.extend(f"• {b}" for b in a.breaking_changes[:5])
        if a.recommended_action:
            lines.append(f"\nAction: {a.recommended_action}")
        if u.release_url:
            lines.append(f"\n{u.release_url}")
        return {
            "title": f"{u.subject}  {u.current_version} → {u.new_version}",
            "message": "\n".join(lines),
            "priority": self._priority_for(a.severity.value),
        }

    async def send(self, item: AnalyzedUpdate) -> bool:
        if not self._should_send(item):
            return True   # nothing to do — don't retry
        url = f"{self.cfg.server_url.rstrip('/')}/message"
        return await self._push_json(
            url, item=item,
            json=self._format(item),
            params={"token": self.cfg.token},
        )
