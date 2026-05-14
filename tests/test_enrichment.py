"""Unit tests for the enrichment fetchers.

Each fetcher is exercised against a mocked `httpx.AsyncClient` (for the
HTTP ones) or a mocked docker container (for logs). We don't hit real
endpoints — the point is to pin parsing, truncation, host detection,
and the "no useful data → None" contract.
"""

from __future__ import annotations

import asyncio
import types

import httpx

from homelabsage.enrichment import (
    _docker_hub_repo_path,
    _is_docker_hub_image,
    fetch_container_logs,
    fetch_docker_hub_description,
    fetch_github_readme,
)


class _MockResponse:
    def __init__(self, status_code: int, text: str = "", json_body=None) -> None:
        self.status_code = status_code
        self.text = text
        self._json_body = json_body

    def json(self):
        if self._json_body is None:
            raise ValueError("not json")
        return self._json_body


class _MockClient:
    """Stand-in for httpx.AsyncClient.

    `script` is a dict: url-substring → list of MockResponse (popped per call).
    Any URL not in the script returns 404.
    """

    def __init__(self, script: dict[str, list[_MockResponse]]) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[str] = []

    async def get(self, url, timeout=None, follow_redirects=False):
        self.calls.append(url)
        for key, queue in self.script.items():
            if key in url and queue:
                return queue.pop(0)
        return _MockResponse(404)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None


# ─── fetch_github_readme ─────────────────────────────────────────────────


def test_github_readme_returns_text_on_200():
    client = _MockClient(
        {
            "/main/README.md": [_MockResponse(200, "# Hello\n\nThe whole project."),],
        }
    )
    text = asyncio.run(
        fetch_github_readme("owner/repo", client=client)
    )
    assert text is not None
    assert "Hello" in text
    assert client.calls[-1].endswith("/main/README.md")


def test_github_readme_falls_back_to_master():
    client = _MockClient(
        {
            "/main/": [
                _MockResponse(404),
                _MockResponse(404),
                _MockResponse(404),
                _MockResponse(404),
            ],
            "/master/README.md": [_MockResponse(200, "# Legacy default branch")],
        }
    )
    text = asyncio.run(fetch_github_readme("owner/repo", client=client))
    assert text == "# Legacy default branch"


def test_github_readme_returns_none_when_no_repo():
    text = asyncio.run(fetch_github_readme(None))
    assert text is None
    text = asyncio.run(fetch_github_readme(""))
    assert text is None
    text = asyncio.run(fetch_github_readme("malformed-no-slash"))
    assert text is None


def test_github_readme_returns_none_on_all_404():
    client = _MockClient({})  # everything misses → 404
    text = asyncio.run(fetch_github_readme("owner/repo", client=client))
    assert text is None


def test_github_readme_returns_none_on_network_failure():
    class _BoomClient(_MockClient):
        async def get(self, url, **kw):
            raise httpx.ConnectError("nope")

    client = _BoomClient({})
    text = asyncio.run(fetch_github_readme("owner/repo", client=client))
    assert text is None


def test_github_readme_truncates_long_responses():
    long_body = "x" * 12000
    client = _MockClient(
        {"/main/README.md": [_MockResponse(200, long_body)]}
    )
    text = asyncio.run(
        fetch_github_readme("owner/repo", max_chars=500, client=client)
    )
    assert text is not None
    assert len(text) <= 500
    assert "truncated" in text


def test_github_readme_skips_empty_200_body():
    client = _MockClient(
        {
            "/main/": [
                _MockResponse(200, "   \n\n"),  # whitespace only
                _MockResponse(200, "   \n\n"),
                _MockResponse(200, "   \n\n"),
                _MockResponse(200, "   \n\n"),
            ],
            "/master/README.md": [_MockResponse(200, "# Real content")],
        }
    )
    text = asyncio.run(fetch_github_readme("owner/repo", client=client))
    assert text == "# Real content"


# ─── _is_docker_hub_image / _docker_hub_repo_path ────────────────────────


def test_is_docker_hub_recognises_canonical_forms():
    assert _is_docker_hub_image("nginx")  # bare library
    assert _is_docker_hub_image("bellamy/wallos")
    assert _is_docker_hub_image("docker.io/bellamy/wallos")
    assert _is_docker_hub_image("index.docker.io/library/redis")


def test_is_docker_hub_rejects_other_registries():
    assert not _is_docker_hub_image("ghcr.io/owner/repo")
    assert not _is_docker_hub_image("lscr.io/linuxserver/mealie")
    assert not _is_docker_hub_image("quay.io/prometheus/node-exporter")


def test_docker_hub_repo_path_handles_library_shortcut():
    assert _docker_hub_repo_path("nginx") == "library/nginx"
    assert _docker_hub_repo_path("redis:7.4") == "library/redis"


def test_docker_hub_repo_path_keeps_namespaced_image():
    assert _docker_hub_repo_path("bellamy/wallos:latest") == "bellamy/wallos"


def test_docker_hub_repo_path_returns_none_for_other_registries():
    assert _docker_hub_repo_path("ghcr.io/owner/repo") is None
    assert _docker_hub_repo_path("lscr.io/linuxserver/mealie") is None


# ─── fetch_docker_hub_description ────────────────────────────────────────


def test_docker_hub_description_parses_full_description():
    client = _MockClient(
        {
            "/v2/repositories/bellamy/wallos/": [
                _MockResponse(
                    200,
                    text="",
                    json_body={"full_description": "Wallos tracks SaaS subs."},
                )
            ]
        }
    )
    text = asyncio.run(
        fetch_docker_hub_description("bellamy/wallos", client=client)
    )
    assert text == "Wallos tracks SaaS subs."


def test_docker_hub_description_skipped_for_non_hub_image():
    client = _MockClient({})
    text = asyncio.run(
        fetch_docker_hub_description("ghcr.io/owner/repo", client=client)
    )
    assert text is None
    assert client.calls == []  # never even tried the HTTP call


def test_docker_hub_description_returns_none_when_empty():
    client = _MockClient(
        {
            "/v2/repositories/bellamy/wallos/": [
                _MockResponse(200, text="", json_body={"full_description": ""})
            ]
        }
    )
    text = asyncio.run(
        fetch_docker_hub_description("bellamy/wallos", client=client)
    )
    assert text is None


def test_docker_hub_description_returns_none_on_404():
    client = _MockClient({})
    text = asyncio.run(
        fetch_docker_hub_description("missing/repo", client=client)
    )
    assert text is None


def test_docker_hub_description_truncates():
    client = _MockClient(
        {
            "/v2/repositories/library/redis/": [
                _MockResponse(
                    200, text="", json_body={"full_description": "X" * 9000}
                )
            ]
        }
    )
    text = asyncio.run(
        fetch_docker_hub_description("redis", max_chars=500, client=client)
    )
    assert text is not None
    assert len(text) <= 500


# ─── fetch_container_logs ────────────────────────────────────────────────


def _fake_container(logs_return, name: str = "openclaw"):
    c = types.SimpleNamespace()
    c.name = name

    def _logs(**kwargs):
        if isinstance(logs_return, Exception):
            raise logs_return
        return logs_return

    c.logs = _logs
    return c


def test_logs_returns_decoded_bytes():
    c = _fake_container(b"first line\nsecond line\n")
    text = fetch_container_logs(c, tail=30)
    assert text is not None
    assert "first line" in text


def test_logs_returns_none_on_exception():
    c = _fake_container(RuntimeError("daemon down"))
    text = fetch_container_logs(c, tail=30)
    assert text is None


def test_logs_returns_none_for_empty_output():
    c = _fake_container(b"   \n\n\t\n")
    text = fetch_container_logs(c, tail=30)
    assert text is None


def test_logs_returns_none_for_none_container():
    assert fetch_container_logs(None) is None


def test_logs_truncates_long_output():
    c = _fake_container(b"line\n" * 1000)
    text = fetch_container_logs(c, tail=30, max_chars=200)
    assert text is not None
    assert len(text) <= 200


def test_logs_replaces_undecodable_bytes_instead_of_crashing():
    c = _fake_container(b"\xff\xfe binary garbage \xc3\x28 mixed in")
    text = fetch_container_logs(c, tail=30)
    assert text is not None
    assert "garbage" in text
