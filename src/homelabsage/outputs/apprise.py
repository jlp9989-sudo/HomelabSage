"""Apprise output — universal push wrapper.

Apprise (`pip install apprise`) is the swiss-army knife of notification
libraries: one consistent `add(url)` API fans out to 100+ services
(Pushover, Mattermost, MS Teams, Slack, Mailgun, AWS SNS, Telegram,
Discord, plus dozens more). For users already on a service we don't
ship a dedicated output for, this is the escape hatch.

Design:
  - The library is OPTIONAL. We import lazily and degrade to a no-op
    with a one-time WARN when the user enables this output without the
    package installed.
  - `urls` is a free-form list — apprise URL syntax is project-defined
    (`pover://USER@TOKEN`, `tgram://BOT/CHAT`, `slack://...`). We don't
    try to validate it.
  - `tag_severity` toggles whether we set the apprise `notify_type` to
    NotifyType.WARNING for high/critical, INFO otherwise. Useful for
    services that visualize urgency natively.
"""

from __future__ import annotations

import logging

from ..models import AnalyzedUpdate, Severity
from . import Output

log = logging.getLogger(__name__)


_WARNED_MISSING = False


def _import_apprise():
    """Import apprise lazily so the package is optional.

    Returns the module or None. The first None return logs a one-time
    WARN so the user knows their config block expected the package.
    """
    global _WARNED_MISSING
    try:
        import apprise as _apprise
        return _apprise
    except ImportError:
        if not _WARNED_MISSING:
            log.warning(
                "outputs.apprise.enabled is true but the `apprise` package "
                "isn't installed. Run `pip install apprise` to activate."
            )
            _WARNED_MISSING = True
        return None


# Apprise NotifyType values are stable across releases. Re-exported as
# strings so we don't need an import at module-import time.
_SEVERITY_TO_TYPE: dict[str, str] = {
    "critical": "failure",
    "high": "warning",
    "medium": "info",
    "info": "info",
}


class AppriseOutput(Output):
    id = "apprise"
    is_push = True

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._min = Severity(cfg.min_severity)

    def _should_send(self, item: AnalyzedUpdate) -> bool:
        if not self.cfg.enabled or not self.cfg.urls:
            return False
        if not item.analysis:
            return False
        return item.analysis.severity.order >= self._min.order

    def _build(self, item: AnalyzedUpdate) -> tuple[str, str]:
        u = item.update
        a = item.analysis
        assert a is not None
        title = f"{u.subject}  {u.current_version} → {u.new_version}"
        parts = [a.summary or "(no summary)"]
        if a.breaking_changes:
            parts.append("\nBreaking:")
            parts.extend(f"• {b}" for b in a.breaking_changes[:5])
        if a.recommended_action:
            parts.append(f"\nAction: {a.recommended_action}")
        if u.release_url:
            parts.append(f"\n{u.release_url}")
        return title, "\n".join(parts)

    async def send(self, item: AnalyzedUpdate) -> None:
        if not self._should_send(item):
            return
        apprise = _import_apprise()
        if apprise is None:
            return
        ap = apprise.Apprise()
        for url in self.cfg.urls:
            try:
                if not ap.add(url):
                    log.warning("apprise rejected URL: %s", url[:48])
            except Exception as e:
                log.warning("apprise add(%s…) failed: %s", url[:48], e)
        title, body = self._build(item)
        notify_type = (
            _SEVERITY_TO_TYPE.get(item.analysis.severity.value, "info")
            if self.cfg.tag_severity and item.analysis else None
        )
        try:
            # `Apprise.notify` is synchronous; the library spawns its own
            # threads for fan-out. We don't run it in `asyncio.to_thread`
            # because each per-channel send is already short and the
            # caller (engine) is fine with sync calls in async output paths.
            kwargs = {"title": title, "body": body}
            if notify_type:
                kwargs["notify_type"] = notify_type
            ok = ap.notify(**kwargs)
            if not ok:
                log.warning("apprise notify reported partial failure for %s", item.id)
        except Exception as e:
            log.error("apprise notify failed for %s: %s", item.id, e)
