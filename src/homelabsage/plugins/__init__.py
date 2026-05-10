"""Plugin base class.

Each plugin = one "source" (Docker, HA, Fedora, llama.cpp, …).
A plugin's only job is to produce a list of `Update` items.
The core handles LLM analysis, storage and routing to outputs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Update


class Plugin(ABC):
    """Base class for all sources."""

    #: Stable plugin id, used as `Update.source` and in DB/config.
    id: str = ""

    @abstractmethod
    async def scan(self) -> list[Update]:
        """Return all updates available right now (idempotent)."""
        raise NotImplementedError
