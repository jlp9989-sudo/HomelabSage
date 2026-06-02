"""Output base class — routes an AnalyzedUpdate to an external destination."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import AnalyzedUpdate


class Output(ABC):
    """Base class for all outputs (Notion, Telegram, webhook, …)."""

    id: str = ""
    #: When True, this output produces a user-visible push notification and
    #: should be skipped while a parity check / resync is running. Persistent
    #: outputs (Notion, the digest dispatcher) keep `is_push = False` because
    #: writing to a DB or pinning a Markdown file is not disruptive.
    is_push: bool = False

    @abstractmethod
    async def send(self, item: AnalyzedUpdate) -> None:
        """Send a single analyzed update. Implementations must be idempotent."""
        raise NotImplementedError
