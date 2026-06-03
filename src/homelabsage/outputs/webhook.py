"""Generic webhook output — POST the analyzed update as JSON.

The escape hatch for everyone whose tool isn't on the apprise list and
isn't email: define a URL, optionally a bearer token + extra headers,
HomelabSage POSTs every update as a JSON envelope. The receiver
parses it however they want (Zapier, n8n, IFTTT WebHooks integration,
self-hosted FastAPI endpoint, whatever).

Envelope shape (stable):

    {
      "version": "0.6.0",
      "type": "homelabsage.update",
      "id": "<source>:<subject>:<new_version>",
      "severity": "high",
      "subject": "mealie",
      "current_version": "1.2.3",
      "new_version": "2.0.0",
      "summary": "Major upgrade ...",
      "breaking_changes": ["env: A → B"],
      "recommended_action": "back up DB",
      "release_url": "https://example/release",
      "context": { ... }
    }
"""

from __future__ import annotations

import logging

import httpx

from .. import __version__
from ..models import AnalyzedUpdate, Severity
from . import Output

log = logging.getLogger(__name__)


class WebhookOutput(Output):
    id = "webhook"
    is_push = True

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._min = Severity(cfg.min_severity)

    def _should_send(self, item: AnalyzedUpdate) -> bool:
        if not self.cfg.enabled or not self.cfg.url:
            return False
        if not item.analysis:
            return False
        return item.analysis.severity.order >= self._min.order

    def _envelope(self, item: AnalyzedUpdate) -> dict:
        u = item.update
        a = item.analysis
        assert a is not None
        return {
            "version": __version__,
            "type": "homelabsage.update",
            "id": item.id,
            "source": u.source,
            "severity": a.severity.value,
            "subject": u.subject,
            "current_version": u.current_version,
            "new_version": u.new_version,
            "summary": a.summary,
            "breaking_changes": list(a.breaking_changes),
            "recommended_action": a.recommended_action,
            "release_url": u.release_url,
            "context": dict(u.context or {}),
        }

    async def send(self, item: AnalyzedUpdate) -> None:
        if not self._should_send(item):
            return
        headers: dict[str, str] = dict(self.cfg.headers or {})
        if self.cfg.bearer_token:
            headers["Authorization"] = f"Bearer {self.cfg.bearer_token}"
        headers.setdefault("Content-Type", "application/json")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    self.cfg.url, json=self._envelope(item), headers=headers,
                )
                r.raise_for_status()
        except httpx.HTTPError as e:
            log.error("webhook push failed for %s: %s", item.id, e)
