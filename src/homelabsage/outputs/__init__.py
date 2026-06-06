"""Output base class — routes an AnalyzedUpdate to an external destination.

Subclasses can re-use the common gate / log helpers / httpx POST wrapper
below instead of reimplementing the same boilerplate per channel:

    class FooOutput(Output):
        id = "foo"
        is_push = True

        def __init__(self, cfg):
            self.cfg = cfg
            self._min = Severity(cfg.min_severity)

        async def send(self, item):
            if not self.cfg.enabled or not self.cfg.url:
                return
            if not self._severity_passes(item):
                return
            await self._push_json(self.cfg.url, json=self._payload(item), item=item)

`_should_send` defaults to `_severity_passes`; outputs with extra preconditions
(URL/token presence, write-policy filters, etc.) override or guard inline.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import httpx

from ..models import AnalyzedUpdate, Severity
from ._errlog import safe_error

log = logging.getLogger(__name__)


class Output(ABC):
    """Base class for all outputs (Notion, Telegram, webhook, …)."""

    id: str = ""
    #: When True, this output produces a user-visible push notification and
    #: should be skipped while a parity check / resync is running. Persistent
    #: outputs (Notion, the digest dispatcher) keep `is_push = False` because
    #: writing to a DB or pinning a Markdown file is not disruptive.
    is_push: bool = False
    #: Severity floor — subclasses set this in `__init__` from `cfg.min_severity`.
    #: Used by `_severity_passes`. Outputs that don't have a severity gate
    #: (Notion) leave it None and never call the helper.
    _min: Severity | None = None

    @abstractmethod
    async def send(self, item: AnalyzedUpdate) -> None:
        """Send a single analyzed update. Implementations must be idempotent."""
        raise NotImplementedError

    # ─── reusable helpers (opt-in from subclasses) ────────────────

    def _severity_passes(self, item: AnalyzedUpdate) -> bool:
        """True iff `item.analysis.severity >= self._min`.

        Returns False when there's no analysis (the LLM hasn't run yet) OR
        the subclass forgot to set `_min` — in both cases the output should
        stay silent rather than guess.
        """
        if item.analysis is None or self._min is None:
            return False
        return item.analysis.severity.order >= self._min.order

    def _log_push_failure(
        self, item: AnalyzedUpdate, exc: BaseException,
    ) -> None:
        """Standard sanitised failure log shared by every push output.

        Uses `safe_error` so we never bleed the request URL (which often
        carries the bot token / webhook secret) into the log file.
        """
        log.error(
            "%s push failed for %s: %s",
            self.id or type(self).__name__, item.id, safe_error(exc),
        )

    async def _push_json(
        self,
        url: str,
        *,
        item: AnalyzedUpdate,
        json: dict | list | None = None,
        params: dict | None = None,
        headers: dict | None = None,
        timeout: float = 15.0,
    ) -> None:
        """Fire-and-forget JSON POST.

        Wraps the httpx try/except/log boilerplate that 9 of the 11 push
        outputs would otherwise reimplement. Catches `httpx.HTTPError`,
        logs via `_log_push_failure`, and returns silently — the caller's
        send path doesn't propagate dispatch failures back to the engine.

        Outputs that need POST + PATCH fallback (Notion) or non-JSON
        bodies (ntfy text/plain, pushover form-encoded) keep their own
        httpx calls.
        """
        # Only pass kwargs the caller set so test doubles can stub a minimal
        # `post(url, json=...)` signature without having to mirror every
        # optional argument.
        post_kwargs: dict = {}
        if json is not None:
            post_kwargs["json"] = json
        if params is not None:
            post_kwargs["params"] = params
        if headers is not None:
            post_kwargs["headers"] = headers
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(url, **post_kwargs)
                r.raise_for_status()
        except httpx.HTTPError as e:
            self._log_push_failure(item, e)
