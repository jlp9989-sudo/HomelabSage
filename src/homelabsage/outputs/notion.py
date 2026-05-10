"""Notion output — write each AnalyzedUpdate as a row in a Notion database.

Expected DB columns (you can tweak via README on first run):
  - Title (title)               → "{subject} {current} → {new}"
  - Source (select)             → docker | homeassistant | ...
  - Severity (select)           → critical | high | medium | info
  - Action required (checkbox)
  - Current (rich_text)
  - New (rich_text)
  - Summary (rich_text)
  - URL (url)
  - Status (select)             → new | analyzed | applied | dismissed | failed
"""

from __future__ import annotations

import logging

import httpx

from ..config import NotionOutputConfig
from ..models import AnalyzedUpdate
from . import Output

log = logging.getLogger(__name__)

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


class NotionOutput(Output):
    id = "notion"

    def __init__(self, cfg: NotionOutputConfig):
        self.cfg = cfg

    def _should_send(self, item: AnalyzedUpdate) -> bool:
        if not self.cfg.enabled:
            return False
        if self.cfg.write_policy == "only_action_required":
            return bool(item.analysis and item.analysis.action_required)
        return True

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def _properties(self, item: AnalyzedUpdate) -> dict:
        u = item.update
        a = item.analysis
        title = f"{u.subject} {u.current_version} → {u.new_version}"
        props: dict = {
            "Title": {"title": [{"text": {"content": title[:200]}}]},
            "Source": {"select": {"name": u.source}},
            "Current": {"rich_text": [{"text": {"content": u.current_version[:100]}}]},
            "New": {"rich_text": [{"text": {"content": u.new_version[:100]}}]},
            "Status": {"select": {"name": item.status.value}},
        }
        if u.release_url:
            props["URL"] = {"url": u.release_url}
        if a:
            props["Severity"] = {"select": {"name": a.severity.value}}
            props["Action required"] = {"checkbox": a.action_required}
            props["Summary"] = {"rich_text": [{"text": {"content": a.summary[:2000]}}]}
        return props

    async def send(self, item: AnalyzedUpdate) -> None:
        if not self._should_send(item):
            return
        payload = {
            "parent": {"database_id": self.cfg.database_id},
            "properties": self._properties(item),
        }
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(
                    f"{NOTION_API}/pages", headers=self._headers(), json=payload
                )
                r.raise_for_status()
        except httpx.HTTPError as e:
            log.error("Notion write failed for %s: %s", item.id, e)
