"""Home Assistant plugin — Core version + HACS integrations (basic).

Reads `/api/config` for the running HA version, then compares against the
latest non-prerelease tag of `home-assistant/core`. HACS integrations are
read from `sensor.hacs` (the official HACS sensor exposes a JSON-friendly
attribute with pending updates).
"""

from __future__ import annotations

import logging

import httpx
from packaging.version import InvalidVersion, Version

from ..config import HAConfig
from ..github import latest_release
from ..models import Update
from . import Plugin

log = logging.getLogger(__name__)


class HomeAssistantPlugin(Plugin):
    id = "homeassistant"

    def __init__(self, cfg: HAConfig):
        self.cfg = cfg

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.cfg.token}", "Content-Type": "application/json"}

    async def _get(self, client: httpx.AsyncClient, path: str) -> dict | list | None:
        url = self.cfg.url.rstrip("/") + path
        try:
            r = await client.get(url, headers=self._headers(), timeout=20)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            log.warning("HA GET %s failed: %s", path, e)
            return None

    async def scan(self) -> list[Update]:
        if not self.cfg.enabled or not self.cfg.token:
            return []

        updates: list[Update] = []
        async with httpx.AsyncClient() as client:
            updates.extend(await self._scan_core(client))
            if self.cfg.include_hacs:
                updates.extend(await self._scan_hacs(client))
        return updates

    # ─── Core ──────────────────────────────────────────────────────────

    async def _scan_core(self, client: httpx.AsyncClient) -> list[Update]:
        cfg_data = await self._get(client, "/api/config")
        if not isinstance(cfg_data, dict):
            return []
        current = (cfg_data.get("version") or "").lstrip("v")
        if not current:
            return []
        release = await latest_release("home-assistant/core")
        if not release:
            return []
        new_version = release.get("tag_name", "").lstrip("v")
        try:
            is_newer = Version(new_version) > Version(current)
        except InvalidVersion:
            is_newer = new_version != current
        if not is_newer:
            return []
        return [
            Update(
                source=self.id,
                subject="core",
                current_version=current,
                new_version=new_version,
                release_url=release.get("html_url"),
                release_notes=release.get("body") or "",
                context={
                    "ha_url": self.cfg.url,
                    "components_loaded": len(cfg_data.get("components", [])),
                },
            )
        ]

    # ─── HACS ──────────────────────────────────────────────────────────

    async def _scan_hacs(self, client: httpx.AsyncClient) -> list[Update]:
        """Read the `sensor.hacs` entity. It exposes a `repositories` attribute
        with installed HACS items and the latest available version."""
        sensor = await self._get(client, "/api/states/sensor.hacs")
        if not isinstance(sensor, dict):
            return []
        repos = sensor.get("attributes", {}).get("repositories", []) or []
        updates: list[Update] = []
        for repo in repos:
            name = repo.get("display_name") or repo.get("name") or "unknown"
            installed = (repo.get("installed_version") or "").lstrip("v")
            available = (repo.get("available_version") or "").lstrip("v")
            if not installed or not available or installed == available:
                continue
            gh_repo = repo.get("name")  # HACS uses `owner/repo` here
            release_url = f"https://github.com/{gh_repo}/releases/tag/{available}" if gh_repo else None
            updates.append(
                Update(
                    source=self.id,
                    subject=f"hacs:{name}",
                    current_version=installed,
                    new_version=available,
                    release_url=release_url,
                    release_notes="",  # filled lazily on demand to avoid 100s of API calls
                    context={"hacs_repo": gh_repo, "category": repo.get("category", "")},
                )
            )
        return updates
