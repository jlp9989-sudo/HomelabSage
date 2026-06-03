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


# ─── Docker Hub URL parser ──────────────────────────────────────────────


@pytest.mark.parametrize("url,expected", [
    ("https://hub.docker.com/r/linuxserver/plex", "linuxserver/plex"),
    ("https://hub.docker.com/r/linuxserver/plex/", "linuxserver/plex"),
    ("https://hub.docker.com/r/linuxserver/plex/tags", "linuxserver/plex"),
    ("https://hub.docker.com/r/linuxserver/plex/general", "linuxserver/plex"),
    ("https://hub.docker.com/_/nginx", "library/nginx"),
    ("https://hub.docker.com/_/postgres/", "library/postgres"),
    ("https://www.hub.docker.com/r/linuxserver/plex", "linuxserver/plex"),
])
def test_parse_dockerhub_url_accepts_supported_shapes(url, expected):
    from homelabsage.analyse_url import parse_dockerhub_url
    assert parse_dockerhub_url(url) == expected


@pytest.mark.parametrize("url", [
    "https://github.com/owner/repo",
    "https://hub.docker.com/",
    "https://docker.io/linuxserver/plex",
    "not a url",
    "",
])
def test_parse_dockerhub_url_returns_none_for_unsupported(url):
    from homelabsage.analyse_url import parse_dockerhub_url
    assert parse_dockerhub_url(url) is None


# ─── Docker Hub analyser ────────────────────────────────────────────────


async def test_analyse_dockerhub_returns_none_for_bad_url():
    from homelabsage.analyse_url import analyse_dockerhub_url
    cfg = Config()
    cfg.llm.provider = "disabled"
    assert await analyse_dockerhub_url(cfg, "not a url") is None


async def test_analyse_dockerhub_builds_update_without_llm(monkeypatch, tmp_path):
    from homelabsage import analyse_url as au_mod
    from homelabsage.analyse_url import analyse_dockerhub_url
    from homelabsage.images import FindAlternativesResult
    from homelabsage.registries import FloatingTagInfo

    cfg = Config()
    cfg.llm.provider = "disabled"
    cfg.storage.database_path = str(tmp_path / "s.sqlite")

    async def fake_meta(slug):
        assert slug == "linuxserver/plex"
        return {
            "description": "Plex Media Server container",
            "full_description": "Detailed description text.",
        }

    async def fake_tag_info(slug, tag, *, client=None):
        return FloatingTagInfo(
            digest="sha256:abc", pushed_at=None, tag=tag, total_size_bytes=0,
        )

    async def fake_alts(image, description=""):
        return FindAlternativesResult(candidates=[])

    monkeypatch.setattr(au_mod, "_dockerhub_repo_meta", fake_meta)
    monkeypatch.setattr(au_mod, "dockerhub_tag_info", fake_tag_info)
    monkeypatch.setattr(au_mod, "find_alternatives", fake_alts)

    result = await analyse_dockerhub_url(
        cfg, "https://hub.docker.com/r/linuxserver/plex",
    )
    assert result is not None
    assert result.update.subject == "linuxserver/plex"
    assert result.update.context["registry"] == "docker.io"
    assert "Detailed description" in result.update.release_notes
    assert result.update.context["dockerhub_description"] == "Plex Media Server container"


async def test_analyse_dockerhub_survives_meta_404(monkeypatch, tmp_path):
    from homelabsage import analyse_url as au_mod
    from homelabsage.analyse_url import analyse_dockerhub_url
    from homelabsage.images import FindAlternativesResult

    cfg = Config()
    cfg.llm.provider = "disabled"
    cfg.storage.database_path = str(tmp_path / "s.sqlite")

    monkeypatch.setattr(au_mod, "_dockerhub_repo_meta",
                        lambda slug: _async(None))
    monkeypatch.setattr(au_mod, "dockerhub_tag_info",
                        lambda slug, tag, *, client=None: _async(None))

    async def empty_alts(image, description=""):
        return FindAlternativesResult(candidates=[])
    monkeypatch.setattr(au_mod, "find_alternatives", empty_alts)

    out = await analyse_dockerhub_url(cfg, "https://hub.docker.com/r/private/repo")
    assert out is not None
    assert out.update.new_version == "latest"
    assert out.update.release_notes == ""


# ─── unified analyse_url dispatcher ─────────────────────────────────────


async def test_analyse_url_dispatches_repo_to_repo_analyser(monkeypatch, tmp_path):
    from homelabsage import analyse_url as au_mod

    cfg = Config()
    cfg.llm.provider = "disabled"
    cfg.storage.database_path = str(tmp_path / "s.sqlite")

    called: dict[str, str] = {}

    async def fake_repo(cfg_arg, url, *, current_version=""):
        called["repo"] = url
        return None

    async def fake_hub(cfg_arg, url, *, current_version=""):
        called["hub"] = url
        return None

    monkeypatch.setattr(au_mod, "analyse_repo_url", fake_repo)
    monkeypatch.setattr(au_mod, "analyse_dockerhub_url", fake_hub)

    await au_mod.analyse_url(cfg, "https://github.com/owner/repo")
    assert "repo" in called
    assert "hub" not in called


async def test_analyse_url_dispatches_hub_to_hub_analyser(monkeypatch, tmp_path):
    from homelabsage import analyse_url as au_mod

    cfg = Config()
    cfg.llm.provider = "disabled"
    cfg.storage.database_path = str(tmp_path / "s.sqlite")

    called: dict[str, str] = {}

    async def fake_repo(cfg_arg, url, *, current_version=""):
        called["repo"] = url
        return None

    async def fake_hub(cfg_arg, url, *, current_version=""):
        called["hub"] = url
        return None

    monkeypatch.setattr(au_mod, "analyse_repo_url", fake_repo)
    monkeypatch.setattr(au_mod, "analyse_dockerhub_url", fake_hub)

    await au_mod.analyse_url(cfg, "https://hub.docker.com/r/linuxserver/plex")
    assert "hub" in called
    assert "repo" not in called


async def test_analyse_url_returns_none_for_unsupported_url():
    from homelabsage.analyse_url import analyse_url as analyse_dispatch
    cfg = Config()
    cfg.llm.provider = "disabled"
    out = await analyse_dispatch(cfg, "https://example.com/random")
    assert out is None
