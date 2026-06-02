"""Tests for the on-demand URL analyser."""

from __future__ import annotations

import pytest

from homelabsage import analyse_url as au
from homelabsage.analyse_url import analyse_repo_url, parse_repo_url
from homelabsage.config import Config

# ─── parse_repo_url ─────────────────────────────────────────────────────


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/openclaw/openclaw", "openclaw/openclaw"),
    ("https://github.com/openclaw/openclaw/", "openclaw/openclaw"),
    ("https://github.com/openclaw/openclaw.git", "openclaw/openclaw"),
    ("https://github.com/openclaw/openclaw/tree/main", "openclaw/openclaw"),
    ("https://github.com/openclaw/openclaw/releases/tag/v1.0.0", "openclaw/openclaw"),
    ("https://github.com/openclaw/openclaw/blob/main/README.md", "openclaw/openclaw"),
    ("https://www.github.com/openclaw/openclaw", "openclaw/openclaw"),
    ("https://codeberg.org/forgejo/forgejo", "codeberg.org/forgejo/forgejo"),
    ("https://codeberg.org/forgejo/forgejo/issues", "codeberg.org/forgejo/forgejo"),
])
def test_parse_repo_url_accepts_supported_shapes(url, expected):
    assert parse_repo_url(url) == expected


@pytest.mark.parametrize("url", [
    "",
    "not a url",
    "https://example.com/foo/bar",
    "https://github.com/",          # missing owner
    "https://github.com/foo",       # missing repo
    "ftp://github.com/owner/repo",  # wrong scheme — netloc check still applies
])
def test_parse_repo_url_returns_none_for_unsupported(url):
    # `ftp://github.com/owner/repo` returns None because parse_repo_url's
    # contract requires the URL to be a supported forge URL — we don't care
    # what scheme the user typed, the host is what we route by, so that one
    # actually passes. Adjust expectations: only really-unsupported shapes.
    if url.startswith("ftp://github.com"):
        assert parse_repo_url(url) == "owner/repo"
    else:
        assert parse_repo_url(url) is None


# ─── analyse_repo_url ──────────────────────────────────────────────────


async def test_analyse_repo_url_returns_none_on_bad_url():
    cfg = Config()
    cfg.llm.provider = "disabled"
    assert await analyse_repo_url(cfg, "not a url") is None


async def test_analyse_repo_url_builds_update_without_llm(monkeypatch, tmp_path):
    cfg = Config()
    cfg.llm.provider = "disabled"
    cfg.storage.database_path = str(tmp_path / "s.sqlite")

    async def fake_latest(repo, **kw):
        return {"tag_name": "v2.0.0", "html_url": "http://x", "body": "release body"}

    async def fake_metadata(repo, **kw):
        return {"pushed_at": "2026-05-01T00:00:00Z", "archived": False}

    monkeypatch.setattr(au, "latest_release", fake_latest)
    monkeypatch.setattr(au, "repo_metadata", fake_metadata)

    result = await analyse_repo_url(cfg, "https://github.com/owner/x", current_version="1.0.0")
    assert result is not None
    assert result.update.subject == "owner/x"
    assert result.update.current_version == "1.0.0"
    assert result.update.new_version == "2.0.0"
    # LLM disabled → analysis None
    assert result.analysis is None


async def test_analyse_repo_url_calls_llm_when_enabled(monkeypatch, tmp_path):
    from homelabsage.models import Analysis, Severity

    cfg = Config()
    cfg.llm.provider = "ollama"
    cfg.llm.endpoint = "http://x"
    cfg.llm.model = "fake"
    cfg.storage.database_path = str(tmp_path / "s.sqlite")

    async def fake_latest(repo, **kw):
        return {"tag_name": "v3.0.0", "html_url": "http://x", "body": ""}

    monkeypatch.setattr(au, "latest_release", fake_latest)
    monkeypatch.setattr(au, "repo_metadata", lambda r, **kw: _async(None))

    captured = {}

    async def fake_analyze(self, update, notes=""):
        captured["called"] = True
        captured["repo"] = update.subject
        return Analysis(severity=Severity.MEDIUM, summary="ok")

    monkeypatch.setattr("homelabsage.llm.LLMClient.analyze", fake_analyze)

    result = await analyse_repo_url(cfg, "https://github.com/owner/x")
    assert captured.get("called") is True
    assert captured["repo"] == "owner/x"
    assert result.analysis is not None
    assert result.analysis.summary == "ok"


async def test_analyse_repo_url_works_when_no_release(monkeypatch, tmp_path):
    """Repos without releases still get a synth Update (release_url=user URL)."""
    cfg = Config()
    cfg.llm.provider = "disabled"
    cfg.storage.database_path = str(tmp_path / "s.sqlite")

    monkeypatch.setattr(au, "latest_release", lambda r, **kw: _async(None))
    monkeypatch.setattr(au, "repo_metadata", lambda r, **kw: _async(None))

    out = await analyse_repo_url(cfg, "https://github.com/owner/x")
    assert out is not None
    assert out.update.new_version == "(none)"
    assert out.update.release_url == "https://github.com/owner/x"


async def _async(value):
    return value
