"""Tests for the news/article URL analyser (sub-case c of /analyse)."""

from __future__ import annotations

import asyncio

from homelabsage import analyse_url as au
from homelabsage.analyse_url import (
    _extract_article_text,
    _strip_html_fallback,
    analyse_article_url,
    looks_like_article_url,
)
from homelabsage.config import Config

# ─── url classifier ──────────────────────────────────────────────────


def test_looks_like_article_url_accepts_https():
    assert looks_like_article_url("https://example.com/post/1")
    assert looks_like_article_url("http://blog.example.org/changelog")


def test_looks_like_article_url_rejects_localhost_and_ip():
    assert not looks_like_article_url("http://localhost:8080/x")
    assert not looks_like_article_url("http://127.0.0.1/")
    assert not looks_like_article_url("file:///etc/passwd")
    assert not looks_like_article_url("ftp://example.com/")


def test_looks_like_article_url_strips_www():
    assert looks_like_article_url("https://www.example.com/")


# ─── extractor ───────────────────────────────────────────────────────


def test_strip_html_fallback_removes_scripts_and_tags():
    html = """
    <html><head><script>evil()</script><style>.x{}</style></head>
    <body><h1>Hi</h1><p>This is body text.</p></body></html>
    """
    out = _strip_html_fallback(html)
    assert "evil" not in out
    assert ".x{" not in out
    assert "Hi" in out
    assert "body text" in out


def test_extract_article_text_uses_fallback_when_trafilatura_missing(monkeypatch):
    """Force the ImportError branch even if trafilatura is installed."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "trafilatura":
            raise ImportError("forced for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    out = _extract_article_text(
        "<html><body><p>The cake is a lie.</p></body></html>",
        url="https://x.example/",
    )
    assert "cake is a lie" in out


# ─── analyser ─────────────────────────────────────────────────────────


def _stub_fetch(html: str | None):
    async def fake_fetch(url: str, *, timeout: float = 20.0):
        return html
    return fake_fetch


def test_analyse_article_url_rejects_short_extraction(monkeypatch):
    monkeypatch.setattr(au, "_fetch_article", _stub_fetch("<html><body>hi</body></html>"))
    cfg = Config()
    result = asyncio.run(analyse_article_url(cfg, "https://x.example/post"))
    assert result is None


def test_analyse_article_url_returns_update_without_llm(monkeypatch):
    big_body = "lorem ipsum " * 60  # > 200 chars after fallback strip
    monkeypatch.setattr(au, "_fetch_article",
                        _stub_fetch(f"<html><body><p>{big_body}</p></body></html>"))
    cfg = Config()  # LLM disabled by default — verify analyzer returns shell
    result = asyncio.run(analyse_article_url(cfg, "https://blog.example/post"))
    assert result is not None
    assert result.update.source == "analyse_url"
    assert result.update.subject == "article:blog.example"
    assert result.analysis is None  # LLM disabled


def test_analyse_article_url_rejects_unfetchable(monkeypatch):
    monkeypatch.setattr(au, "_fetch_article", _stub_fetch(None))
    cfg = Config()
    assert asyncio.run(analyse_article_url(cfg, "https://x.example/")) is None


def test_dispatch_routes_to_article_when_not_repo_or_hub(monkeypatch):
    big_body = "lorem ipsum " * 60
    monkeypatch.setattr(au, "_fetch_article",
                        _stub_fetch(f"<html><body>{big_body}</body></html>"))
    cfg = Config()
    result = asyncio.run(au.analyse_url(cfg, "https://news.example/2026/06/x"))
    assert result is not None
    assert result.update.subject.startswith("article:")


def test_dispatch_repo_still_wins_for_github(monkeypatch):
    # Force the github path to short-circuit: stub latest_release to None.
    async def fake_latest(repo):
        return None
    monkeypatch.setattr(au, "latest_release", fake_latest)
    cfg = Config()
    result = asyncio.run(au.analyse_url(cfg, "https://github.com/owner/repo"))
    # Article path would have rejected this with looks_like_article_url
    # passing through; instead the repo path took it.
    assert result is not None
    assert result.update.source == "analyse_url"
    assert result.update.subject == "owner/repo"
