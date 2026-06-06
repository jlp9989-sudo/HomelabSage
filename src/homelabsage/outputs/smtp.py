"""SMTP / email output — direct, stdlib-only.

Why SMTP instead of relying on apprise for everything: a lot of homelab
users already have a working SMTP relay (their ISP's, an authenticated
Mailgun account, the postfix container they run for the family). One
more push channel that doesn't need a 3rd-party library is welcome.

Design:
  - Single recipient per email body. If the user lists multiple
    `to_addrs`, each gets the same message in the To header (one BCC
    fan-out would be a marginal optimisation; the LLM cost
    dominates).
  - STARTTLS is opt-in by default; `port: 587` triggers STARTTLS,
    `port: 465` triggers implicit-SSL, anything else stays cleartext.
  - Auth is optional — many homelab relays inside the LAN don't need it.
  - We never inline HTML; the body is plain text so the message
    renders identically across every mail client.
"""

from __future__ import annotations

import contextlib
import logging
import smtplib
from email.message import EmailMessage

from ..models import AnalyzedUpdate, Severity
from . import Output

log = logging.getLogger(__name__)


class SMTPOutput(Output):
    id = "smtp"
    is_push = True

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._min = Severity(cfg.min_severity)

    def _should_send(self, item: AnalyzedUpdate) -> bool:
        if not self.cfg.enabled:
            return False
        if not self.cfg.host or not self.cfg.from_addr or not self.cfg.to_addrs:
            return False
        return self._severity_passes(item)

    def _build_message(self, item: AnalyzedUpdate, recipient: str) -> EmailMessage:
        u = item.update
        a = item.analysis
        assert a is not None
        msg = EmailMessage()
        prefix = self.cfg.subject_prefix or "[HomelabSage]"
        msg["Subject"] = (
            f"{prefix} {a.severity.value.upper()} — {u.subject} "
            f"{u.current_version} → {u.new_version}"
        )
        msg["From"] = self.cfg.from_addr
        msg["To"] = recipient
        body_parts = [
            f"Severity: {a.severity.value}",
            f"Summary:  {a.summary or '(no summary)'}",
        ]
        if a.breaking_changes:
            body_parts.append("\nBreaking changes:")
            body_parts.extend(f"  - {b}" for b in a.breaking_changes[:10])
        if a.recommended_action:
            body_parts.append(f"\nRecommended action:\n  {a.recommended_action}")
        if u.release_url:
            body_parts.append(f"\nRelease URL: {u.release_url}")
        body_parts.append(f"\nUpdate ID: {item.id}")
        msg.set_content("\n".join(body_parts))
        return msg

    def _open(self) -> smtplib.SMTP:
        """Return a connected, configured smtplib client.

        Caller is responsible for `.quit()`. We pick the right transport
        based on port: 465 → implicit SSL, 587 → STARTTLS upgrade,
        everything else → plain cleartext.
        """
        host = self.cfg.host
        port = int(self.cfg.port or 25)
        timeout = float(self.cfg.timeout or 30)
        if port == 465:
            client: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=timeout)
        else:
            client = smtplib.SMTP(host, port, timeout=timeout)
            client.ehlo()
            if port == 587 or self.cfg.use_tls:
                client.starttls()
                client.ehlo()
        if self.cfg.username and self.cfg.password:
            client.login(self.cfg.username, self.cfg.password)
        return client

    async def send(self, item: AnalyzedUpdate) -> None:
        if not self._should_send(item):
            return
        import asyncio
        try:
            await asyncio.to_thread(self._send_sync, item)
        except Exception as e:
            self._log_push_failure(item, e)

    def _send_sync(self, item: AnalyzedUpdate) -> None:
        """Synchronous send — runs in a worker thread so the engine's
        event loop doesn't block on STARTTLS handshakes."""
        client = self._open()
        try:
            for recipient in self.cfg.to_addrs:
                msg = self._build_message(item, recipient)
                client.send_message(msg)
        finally:
            with contextlib.suppress(Exception):
                client.quit()
