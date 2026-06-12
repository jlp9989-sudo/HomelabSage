"""SSRF guard: public-host validation + per-redirect-hop enforcement."""

from __future__ import annotations

import ipaddress

import httpx
import pytest

from homelabsage.safe_url import (
    BlockedURLError,
    _ip_is_public,
    check_url_public,
    fetch_text_guarded,
)

# ─── _ip_is_public ────────────────────────────────────────────────


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1",
    "169.254.169.254",          # cloud metadata
    "0.0.0.0", "::1", "fc00::1", "fe80::1",
])
def test_private_and_special_ips_rejected(ip):
    assert _ip_is_public(ipaddress.ip_address(ip)) is False


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_public_ips_accepted(ip):
    assert _ip_is_public(ipaddress.ip_address(ip)) is True


# ─── check_url_public ─────────────────────────────────────────────


def test_check_rejects_non_http_scheme():
    with pytest.raises(BlockedURLError):
        check_url_public("file:///etc/passwd")
    with pytest.raises(BlockedURLError):
        check_url_public("gopher://example.com/")


def test_check_rejects_literal_private_ip():
    with pytest.raises(BlockedURLError):
        check_url_public("http://169.254.169.254/latest/meta-data/")
    with pytest.raises(BlockedURLError):
        check_url_public("http://192.168.1.1/admin")


def test_check_rejects_loopback_hostname(monkeypatch):
    # localhost resolves to 127.0.0.1 — must be blocked by resolution.
    assert check_url_public.__name__  # smoke
    with pytest.raises(BlockedURLError):
        check_url_public("http://localhost:8080/")


def test_check_rejects_host_resolving_to_private(monkeypatch):
    import homelabsage.safe_url as mod
    monkeypatch.setattr(
        mod.socket, "getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("10.1.2.3", 0))],
    )
    with pytest.raises(BlockedURLError):
        check_url_public("http://rebind.evil.test/")


def test_check_rejects_when_any_answer_is_private(monkeypatch):
    """DNS-rebinding shape: one public + one private A record → reject."""
    import homelabsage.safe_url as mod
    monkeypatch.setattr(
        mod.socket, "getaddrinfo",
        lambda host, port: [
            (2, 1, 6, "", ("8.8.8.8", 0)),
            (2, 1, 6, "", ("127.0.0.1", 0)),
        ],
    )
    with pytest.raises(BlockedURLError):
        check_url_public("http://mixed.test/")


def test_check_accepts_public_host(monkeypatch):
    import homelabsage.safe_url as mod
    monkeypatch.setattr(
        mod.socket, "getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    check_url_public("https://example.com/article")   # no raise


# ─── fetch_text_guarded ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_blocks_private_target(monkeypatch):
    called = False

    async def _never(*a, **k):
        nonlocal called
        called = True
        raise AssertionError("must not fetch a blocked URL")

    monkeypatch.setattr(httpx.AsyncClient, "get", _never)
    out = await fetch_text_guarded("http://169.254.169.254/")
    assert out is None
    assert called is False


@pytest.mark.asyncio
async def test_fetch_blocks_redirect_into_private(monkeypatch):
    """A public URL that 302s to an internal address must be stopped at
    the hop, not followed."""
    import homelabsage.safe_url as mod

    # All hostnames "resolve public" so the guard relies on the literal-IP
    # check for the redirect target.
    monkeypatch.setattr(
        mod.socket, "getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )

    hops: list[str] = []

    async def _fake_get(self, url, headers=None):
        hops.append(str(url))
        req = httpx.Request("GET", url)
        # First hop: redirect to a private IP. Guard must block hop 2.
        return httpx.Response(
            302, headers={"location": "http://10.0.0.5/secret"}, request=req,
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)
    out = await fetch_text_guarded("https://example.com/start")
    assert out is None
    assert hops == ["https://example.com/start"]   # never fetched the 10.x


@pytest.mark.asyncio
async def test_fetch_returns_body_on_public_200(monkeypatch):
    import homelabsage.safe_url as mod
    monkeypatch.setattr(
        mod.socket, "getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )

    async def _fake_get(self, url, headers=None):
        req = httpx.Request("GET", url)
        return httpx.Response(200, text="<html>hi</html>", request=req)

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)
    out = await fetch_text_guarded("https://example.com/article")
    assert out == "<html>hi</html>"
