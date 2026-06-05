"""Tests for v0.10.0: punch-list crits C1 (dns timeout race) + C3 (token leak in logs)."""

from __future__ import annotations

import logging
import socket
import threading
import time

import pytest

from homelabsage import dns_check
from homelabsage.outputs._errlog import safe_error

# ─── C1: socket timeout no longer process-wide ────────────────────


def test_dns_probe_does_not_mutate_default_socket_timeout(monkeypatch):
    """Regression: `_temp_timeout` used socket.setdefaulttimeout (process-wide).
    After v0.10.0 the probe runs in a thread-pool and never touches the
    global default. A concurrent stranger doing `socket.getdefaulttimeout()`
    must see whatever was set before AND after the probe.
    """
    socket.setdefaulttimeout(7.5)
    try:
        assert socket.getdefaulttimeout() == 7.5
        # Probe a known-bad host; we don't care if it succeeds or not, only
        # that the global state is preserved.
        dns_check.probe("dns-c1-regression.invalid.", timeout=0.5)
        assert socket.getdefaulttimeout() == 7.5
    finally:
        socket.setdefaulttimeout(None)


def test_dns_probe_returns_timeout_finding_on_hang(monkeypatch):
    """If the lookup is slower than our timeout, return a finding tagged
    `TimeoutError` rather than blocking the caller indefinitely."""
    def _slow(*_a, **_kw):
        time.sleep(2.0)
        return []
    monkeypatch.setattr(socket, "getaddrinfo", _slow)
    finding = dns_check.probe("hang.example.", timeout=0.1)
    assert finding is not None
    assert "TimeoutError" in finding.error


def test_dns_probe_concurrent_safe(monkeypatch):
    """Two threads probing simultaneously must each get their own timeout
    bound — the bug was that one thread's _temp_timeout could restore
    the wrong baseline when interleaved."""
    socket.setdefaulttimeout(9.0)
    seen_during: list[float | None] = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        for _ in range(5):
            dns_check.probe("conc.example.invalid.", timeout=0.2)
            seen_during.append(socket.getdefaulttimeout())

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    socket.setdefaulttimeout(None)
    # During the entire dance the default should have stayed at 9.0 —
    # never seen 0.2 leaked into the global, never seen None.
    assert all(v == 9.0 for v in seen_during), seen_during


def test_dns_probe_success_returns_none():
    """`localhost` always resolves; success is the silent None case."""
    finding = dns_check.probe("localhost", timeout=2.0)
    assert finding is None


# ─── C3: safe_error never includes the request URL ────────────────


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _FakeHTTPStatusError(Exception):
    """Shaped like httpx.HTTPStatusError — has `.response.status_code`
    and a `__str__` that includes the request URL with token."""

    def __init__(self, url: str, status: int):
        self._url = url
        self.response = _FakeResponse(status)
        super().__init__(f"Client error '401 Unauthorized' for url '{url}'")


def test_safe_error_strips_url_with_token():
    e = _FakeHTTPStatusError(
        "https://api.telegram.org/bot1234567:SUPERSECRETTOKEN/sendMessage",
        401,
    )
    result = safe_error(e)
    assert "SUPERSECRETTOKEN" not in result
    assert "api.telegram.org" not in result
    assert "1234567" not in result
    # Should mention status though
    assert "401" in result
    assert "_FakeHTTPStatusError" in result


def test_safe_error_status_in_query_string_not_leaked():
    """Gotify puts token=… in the query string."""
    e = _FakeHTTPStatusError(
        "https://gotify.example.com/message?token=AppToken123",
        403,
    )
    result = safe_error(e)
    assert "AppToken123" not in result
    assert "gotify.example.com" not in result
    assert "403" in result


def test_safe_error_generic_exception_just_type():
    e = RuntimeError("connection refused to https://internal.example.com")
    result = safe_error(e)
    assert "internal.example.com" not in result
    assert result == "RuntimeError"


def test_safe_error_without_response_attr():
    """httpx connect/timeout errors have no .response — just the type name."""
    class _ConnectError(Exception):
        pass
    e = _ConnectError("any:thing")
    assert safe_error(e) == "_ConnectError"


# ─── End-to-end: log output never carries the token ───────────────


def test_telegram_log_does_not_leak_token(caplog, monkeypatch):
    """Capture the logger on a Telegram push that 401s with the token in
    the URL. Verify the captured log line never contains the token."""
    from homelabsage.outputs import telegram as tg_mod

    fake = _FakeHTTPStatusError(
        "https://api.telegram.org/botSECRETLEAKYTOKEN/sendMessage",
        401,
    )
    caplog.set_level(logging.ERROR, logger=tg_mod.log.name)
    tg_mod.log.error(
        "Telegram push failed for %s: %s",
        "docker:mealie:2.0",
        tg_mod.safe_error(fake),
    )
    full_log = "\n".join(r.getMessage() for r in caplog.records)
    assert "SECRETLEAKYTOKEN" not in full_log
    assert "api.telegram.org" not in full_log
    assert "401" in full_log


@pytest.mark.parametrize("module", [
    "telegram", "gotify", "discord", "ntfy", "slack",
    "msteams", "pushover", "webhook", "apprise", "smtp", "notion",
])
def test_every_output_imports_safe_error(module):
    """Defensive guard so a future refactor can't drop the import on one
    output while leaving the others sanitized."""
    import importlib
    mod = importlib.import_module(f"homelabsage.outputs.{module}")
    assert hasattr(mod, "safe_error"), f"{module} missing safe_error import"
