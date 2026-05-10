"""Output base class — routes an AnalyzedUpdate to an external destination."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import AnalyzedUpdate


class Output(ABC):
    """Base class for all outputs (Notion, Telegram, webhook, …)."""

    id: str = ""

    @abstractmethod
    async def send(self, item: AnalyzedUpdate) -> None:
        """Send a single analyzed update. Implementations must be idempotent."""
        raise NotImplementedError
