"""Ntfy output — push notifications via the ntfy.sh protocol.

Ntfy is the self-hosted notification king on r/selfhosted: one POST to a
topic URL fans out to every device subscribed to that topic, no account,
no auth (unless the user enables it). Works against ntfy.sh, a docker
self-host, or any compatible server.

Topic discovery: the user picks ANY string they like (e.g. `homelab-alerts`)
and subscribes to `https://<server>/<topic>` from the mobile/web app. Whoever
knows the topic name can publish *and* read, so it's effectively a shared
secret — pick something random for anything sensitive.

Auth: when the user has set up access tokens on a self-hosted ntfy, the
plugin sends `Authorization: Bearer <token>`. Basic-auth is also supported
by the same header shape but is not exposed as a separate field.

Format:
  - Title:    `<subject> <current> → <new>`
  - Body:     LLM summary plus optional breaking-change bullets
  - Tags:     severity emoji (mirrors Telegram), source tag for filtering
  - Priority: ntfy 1-5 scale derived from Severity
  - Click:    set to `release_url` so tapping the notification opens the changelog
"""

from __future__ import annotations

import logging

import httpx

from ..config import NtfyOutputConfig
from ..models import AnalyzedUpdate, Severity
from . import Output

log = logging.getLogger(__name__)


# ntfy priority is 1 (min) to 5 (max). Map severities so "critical" wakes the
# phone via the default Android channel while "info" is silent-bundle level.
_SEVERITY_PRIORITY: dict[str, int] = {
    "critical": 5,
    "high": 4,
    "medium": 3,
    "info": 2,
}

# Per ntfy docs each tag becomes an emoji prefix in the title. The strings
# below match what ntfy.sh's emoji map recognises today (kept short to avoid
# title bloat on small lock screens).
_SEVERITY_TAGS: dict[str, str] = {
    "critical": "rotating_light",
    "high": "warning",
    "medium": "bell",
    "info": "information_source",
}


class NtfyOutput(Output):
    id = "ntfy"
    is_push = True

    def __init__(self, cfg: NtfyOutputConfig):
        self.cfg = cfg
        self._min = Severity(cfg.min_severity)

    def _should_send(self, item: AnalyzedUpdate) -> bool:
        if not self.cfg.enabled or not self.cfg.server_url or not self.cfg.topic:
            return False
        if not item.analysis:
            return False
        return item.analysis.severity.order >= self._min.order

    def _topic_url(self) -> str:
        return f"{self.cfg.server_url.rstrip('/')}/{self.cfg.topic.lstrip('/')}"

    def _build(self, item: AnalyzedUpdate) -> tuple[str, dict[str, str]]:
        u = item.update
        a = item.analysis
        assert a is not None  # checked in _should_send
        body_parts = [a.summary or "(no summary)"]
        if a.breaking_changes:
            body_parts.append("\nBreaking:")
            body_parts.extend(f"• {b}" for b in a.breaking_changes[:5])
        if a.recommended_action:
            body_parts.append(f"\nAction: {a.recommended_action}")
        body = "\n".join(body_parts)

        tags = [_SEVERITY_TAGS.get(a.severity.value, "bell"), u.source]
        headers: dict[str, str] = {
            "Title": f"{u.subject}  {u.current_version} → {u.new_version}",
            "Tags": ",".join(tags),
            "Priority": str(_SEVERITY_PRIORITY.get(a.severity.value, 3)),
        }
        if u.release_url:
            headers["Click"] = u.release_url
        if self.cfg.auth_token:
            headers["Authorization"] = f"Bearer {self.cfg.auth_token}"
        return body, headers

    async def send(self, item: AnalyzedUpdate) -> None:
        if not self._should_send(item):
            return
        body, headers = self._build(item)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # ntfy accepts the message body as a plain-text POST. Encode
                # explicitly so unicode (emoji, accented service names) round-
                # trips correctly via the Title header too.
                r = await client.post(
                    self._topic_url(),
                    content=body.encode("utf-8"),
                    headers=headers,
                )
                r.raise_for_status()
        except httpx.HTTPError as e:
            log.error("Ntfy push failed for %s: %s", item.id, e)
