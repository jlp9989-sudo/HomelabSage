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
from ._errlog import safe_error

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
        # I8 (v1.0 punch list): scrub the context dict before sending.
        # `Update.context` carries `puid_pgid`, `env` from docker config,
        # raw `release_notes` from upstream — anyone with the webhook
        # URL would otherwise receive an inventory of homelab secrets.
        # `redact_context` walks the tree, replaces secret-key values
        # with the placeholder and runs the text passes on every string.
        # Also scrub the post-LLM analyzer fields (summary,
        # breaking_changes, recommended_action) — same logic as v0.10.3
        # Notion fix, applied here for symmetry across push channels.
        from ..secret_guard import redact_context, redact_text
        safe_summary, _ = redact_text(a.summary or "")
        safe_action, _ = redact_text(a.recommended_action or "") if a.recommended_action else ("", None)
        safe_breaking = []
        for bc in (a.breaking_changes or []):
            cleaned, _ = redact_text(str(bc))
            safe_breaking.append(cleaned)
        safe_context, _ = redact_context(dict(u.context or {}))
        return {
            "version": __version__,
            "type": "homelabsage.update",
            "id": item.id,
            "source": u.source,
            "severity": a.severity.value,
            "subject": u.subject,
            "current_version": u.current_version,
            "new_version": u.new_version,
            "summary": safe_summary,
            "breaking_changes": safe_breaking,
            "recommended_action": safe_action or None,
            "release_url": u.release_url,
            "context": safe_context,
        }

    async def send(self, item: AnalyzedUpdate) -> None:
        if not self._should_send(item):
            return
        # Validate user-supplied header k/v pairs: HTTP headers must not
        # contain CR/LF (header injection / response splitting). Drop
        # offenders silently — the user typoed a template variable into
        # a header, the right behaviour is "headers without the bad
        # entry, log it" rather than blowing up the dispatch.
        headers: dict[str, str] = {}
        for k, v in (self.cfg.headers or {}).items():
            if not isinstance(k, str) or not isinstance(v, str):
                continue
            if "\r" in k or "\n" in k or "\r" in v or "\n" in v:
                log.warning(
                    "webhook: dropping header %r — CR/LF in name or value",
                    k,
                )
                continue
            headers[k] = v
        if self.cfg.bearer_token:
            # Token is set from config; we still validate it for CRLF
            # because the source might be a templated env var.
            tok = str(self.cfg.bearer_token)
            if "\r" not in tok and "\n" not in tok:
                headers["Authorization"] = f"Bearer {tok}"
        headers.setdefault("Content-Type", "application/json")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    self.cfg.url, json=self._envelope(item), headers=headers,
                )
                r.raise_for_status()
        except httpx.HTTPError as e:
            log.error("webhook push failed for %s: %s", item.id, safe_error(e))
