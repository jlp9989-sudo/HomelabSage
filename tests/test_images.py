"""Unit tests for the alternative-image detector.

Pure helpers + the filter pipeline are exercised directly. The orchestrator
`find_alternatives` is integration-tested against an `httpx.MockTransport`
that emulates Docker Hub + LSIO responses, so we cover the HTTP plumbing
without touching the real network.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from homelabsage.images import (
    Alternative,
    Criteria,
    _filter_candidate,
    _has_recent_semver_tag,
    _hub_result_to_alt,
    _jaccard,
    _lsio_row_to_alt,
    _parse_dt,
    _short_name,
    _tokenise,
    find_alternatives,
)

# ─── pure helpers ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "image, expected",
    [
        ("ghcr.io/imagegenius/immich:openvino-1.155", "immich"),
        ("imagegenius/immich:latest", "immich"),
        ("immich-app/immich-server:v1.155", "immich-server"),
        ("jellyfin/jellyfin", "jellyfin"),
        ("library/postgres:16", "postgres"),
        ("registry.example.com:5000/team/whatever:1.0", "whatever"),
    ],
)
def test_short_name(image, expected):
    assert _short_name(image) == expected


def test_tokenise_filters_stopwords_and_short_tokens():
    out = _tokenise("Self-hosted photo and video server")
    # "self-hosted", "and", "server" are stopwords; "a" too short
    assert "photo" in out and "video" in out
    assert "self" not in out and "and" not in out and "server" not in out


def test_tokenise_handles_empty():
    assert _tokenise("") == set()
    assert _tokenise(None) == set()


def test_jaccard_identical_is_one():
    assert _jaccard({"a", "b"}, {"a", "b"}) == 1.0


def test_jaccard_disjoint_is_zero():
    assert _jaccard({"a"}, {"b"}) == 0.0


def test_jaccard_both_empty_is_zero():
    assert _jaccard(set(), set()) == 0.0


def test_jaccard_partial_overlap():
    # {a,b,c} vs {b,c,d} → 2/4 = 0.5
    assert _jaccard({"a", "b", "c"}, {"b", "c", "d"}) == 0.5


def test_has_recent_semver_tag_picks_first_match():
    tags = ["latest", "main", "v1.2.3", "edge"]
    assert _has_recent_semver_tag(tags) == "v1.2.3"


def test_has_recent_semver_tag_returns_none_when_no_semver():
    tags = ["latest", "main", "edge"]
    assert _has_recent_semver_tag(tags) is None


def test_parse_dt_accepts_z_and_offset():
    assert _parse_dt("2025-04-12T10:33:45Z") is not None
    assert _parse_dt("2025-04-12T10:33:45.123456Z") is not None
    assert _parse_dt("2025-04-12T10:33:45+02:00") is not None
    # Nanoseconds — must be truncated
    assert _parse_dt("2025-04-12T10:33:45.987654321+02:00") is not None


def test_parse_dt_returns_none_on_garbage():
    assert _parse_dt("") is None
    assert _parse_dt(None) is None
    assert _parse_dt("not-a-timestamp") is None


# ─── shape normalisation ────────────────────────────────────────────────

def test_hub_result_to_alt_basic():
    alt = _hub_result_to_alt(
        {
            "repo_name": "linuxserver/jellyfin",
            "short_description": "Free Software Media System",
            "pull_count": 50_000_000,
            "last_updated": "2026-05-01T10:00:00.000000Z",
        }
    )
    assert alt is not None
    assert alt.image == "linuxserver/jellyfin"
    assert alt.pulls == 50_000_000
    assert alt.source == "docker-hub"


def test_hub_result_implicit_library_namespace():
    """Docker official images are returned without a namespace."""
    alt = _hub_result_to_alt(
        {
            "name": "postgres",
            "pull_count": 1_000_000_000,
            "last_updated": "2026-05-01T10:00:00Z",
        }
    )
    assert alt is not None
    assert alt.image == "library/postgres"


def test_hub_result_to_alt_returns_none_without_timestamp():
    assert _hub_result_to_alt({"repo_name": "x/y", "pull_count": 1}) is None


def test_lsio_row_to_alt_basic():
    alt = _lsio_row_to_alt(
        {
            "name": "jellyfin",
            "description": "Jellyfin media server",
            "pulls": 10_000_000,
            "updated_at": "2026-05-01T10:00:00Z",
            "github_url": "https://github.com/jellyfin/jellyfin",
        }
    )
    assert alt is not None
    assert alt.image == "linuxserver/jellyfin"
    assert alt.source == "lsio"
    assert alt.github_url == "https://github.com/jellyfin/jellyfin"


# ─── filter pipeline ────────────────────────────────────────────────────

def _alt(
    image: str,
    *,
    pulls: int = 5_000_000,
    age_days: int = 10,
    desc: str = "",
    source: str = "docker-hub",
) -> Alternative:
    return Alternative(
        image=image,
        pulls=pulls,
        last_pushed=datetime.now(UTC) - timedelta(days=age_days),
        source=source,
        short_description=desc,
    )


def _filt(cand: Alternative, **overrides) -> bool:
    defaults: dict = dict(
        current_image="imagegenius/immich",
        current_short="immich",
        current_short_tokens={"immich"},
        current_pulls=100_000,
        current_description="Self-hosted photo and video backup",
        crit=Criteria(),
        now=datetime.now(UTC),
    )
    defaults.update(overrides)
    return _filter_candidate(cand, **defaults)


def test_filter_accepts_same_short_name_different_namespace():
    """The textbook case: linuxserver/immich vs imagegenius/immich."""
    cand = _alt("linuxserver/immich", pulls=20_000_000, age_days=5)
    assert _filt(cand) is True


def test_filter_rejects_self():
    cand = _alt("imagegenius/immich", pulls=20_000_000, age_days=5)
    assert _filt(cand) is False


def test_filter_rejects_stale_candidate():
    cand = _alt("linuxserver/immich", pulls=20_000_000, age_days=200)
    assert _filt(cand) is False


def test_filter_rejects_insufficient_pull_ratio():
    """Current pulls 100k, candidate only 500k → 5×, below 10× threshold."""
    cand = _alt("linuxserver/immich", pulls=500_000, age_days=5)
    assert _filt(cand, current_pulls=100_000) is False


def test_filter_uses_absolute_fallback_when_current_pulls_unknown():
    """GHCR-hosted images have no pull count, so we gate on absolute pulls."""
    cand_low = _alt("popular/immich", pulls=500_000, age_days=5)
    cand_high = _alt("popular/immich", pulls=5_000_000, age_days=5)
    assert _filt(cand_low, current_pulls=None) is False
    assert _filt(cand_high, current_pulls=None) is True


def test_filter_rejects_unrelated_name_even_when_popular():
    """`nginx` is huge but unrelated to `immich` — must not be proposed."""
    cand = _alt("nginx/nginx", pulls=1_000_000_000, age_days=1, desc="HTTP server")
    assert _filt(cand) is False


def test_filter_accepts_via_description_overlap_when_name_diverges():
    cand = _alt(
        "photoprism/photoprism",  # different short name
        pulls=5_000_000,
        age_days=5,
        desc="Self-hosted photo backup with face recognition",
    )
    assert _filt(cand) is True


def test_filter_rejects_recent_but_too_small():
    cand = _alt("tinyfork/immich", pulls=50_000, age_days=1)
    assert _filt(cand, current_pulls=100_000) is False


# ─── orchestrator with mocked HTTP ─────────────────────────────────────

def _build_mock_transport(*, hub_rows: list[dict], lsio_rows: list[dict],
                          current_pulls: int | None = None,
                          tags_by_repo: dict[str, list[str]] | None = None,
                          ) -> httpx.MockTransport:
    """Build a transport that responds to the four endpoints `find_alternatives` hits."""
    tags_by_repo = tags_by_repo or {}

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path  # path only, query params excluded
        if host == "hub.docker.com" and path.startswith("/v2/search/repositories"):
            return httpx.Response(200, json={"results": hub_rows})
        if host == "fleet.linuxserver.io" and path.startswith("/api/v1/images"):
            return httpx.Response(200, json={"data": lsio_rows})
        if host == "hub.docker.com" and path.startswith("/v2/repositories/"):
            tail = path[len("/v2/repositories/"):]
            # `<owner>/<name>/tags/` vs `<owner>/<name>/` — same prefix, different suffix
            if tail.endswith("/tags/"):
                repo = tail[: -len("/tags/")]
                return httpx.Response(200, json={
                    "results": [{"name": t} for t in tags_by_repo.get(repo, [])]
                })
            if current_pulls is None:
                return httpx.Response(404)
            return httpx.Response(200, json={"pull_count": current_pulls})
        return httpx.Response(404, text="unhandled in mock")

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_find_alternatives_returns_high_value_only():
    """End-to-end against a realistic synthetic catalogue."""
    now = datetime.now(UTC)
    recent = (now - timedelta(days=10)).isoformat().replace("+00:00", "Z")
    stale = (now - timedelta(days=300)).isoformat().replace("+00:00", "Z")
    hub_rows = [
        {
            "repo_name": "linuxserver/jellyfin",
            "short_description": "Jellyfin media server",
            "pull_count": 50_000_000,
            "last_updated": recent,
        },
        {
            # Stale: should be filtered out by recent_push_days
            "repo_name": "abandoned/jellyfin-fork",
            "short_description": "Old fork",
            "pull_count": 60_000_000,
            "last_updated": stale,
        },
        {
            # Below absolute pulls floor
            "repo_name": "smallfork/jellyfin-mini",
            "short_description": "Personal fork of jellyfin media server",
            "pull_count": 5_000,
            "last_updated": recent,
        },
        {
            # Unrelated despite being popular
            "repo_name": "library/nginx",
            "short_description": "Web server",
            "pull_count": 9_000_000_000,
            "last_updated": recent,
        },
    ]
    lsio_rows = [
        {
            "name": "jellyfin",
            "description": "Jellyfin media server",
            "pulls": 80_000_000,
            "updated_at": recent,
            "github_url": "https://github.com/jellyfin/jellyfin",
        }
    ]
    tags_by_repo = {"linuxserver/jellyfin": ["latest", "10.10.3", "10.10.2"]}

    transport = _build_mock_transport(
        hub_rows=hub_rows, lsio_rows=lsio_rows, tags_by_repo=tags_by_repo,
        current_pulls=None,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        result = await find_alternatives(
            "ghcr.io/imagegenius/jellyfin",
            description="Jellyfin media server",
            client=client,
        )

    names = [a.image for a in result.candidates]
    assert "library/nginx" not in names              # unrelated → rejected
    assert "abandoned/jellyfin-fork" not in names    # stale → rejected
    assert "smallfork/jellyfin-mini" not in names    # too small → rejected
    # The popular & recent ones survive (both LSIO entry and the Hub linuxserver entry):
    assert any("jellyfin" in n.lower() for n in names)
    # The sample_tag enrichment must run on at least one survivor.
    assert any(a.sample_tag == "10.10.3" for a in result.candidates)
    assert result.sources_consulted == ["docker-hub", "lsio"]


@pytest.mark.asyncio
async def test_find_alternatives_returns_empty_when_no_candidates_pass():
    transport = _build_mock_transport(
        hub_rows=[],
        lsio_rows=[],
        current_pulls=None,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        result = await find_alternatives("ghcr.io/x/y", client=client)
    assert result.candidates == []
    assert result.sources_consulted == ["docker-hub", "lsio"]


@pytest.mark.asyncio
async def test_find_alternatives_swallows_http_errors():
    """If a source 500s, we keep going with whatever the other source returned."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "fleet.linuxserver" in str(request.url):
            return httpx.Response(500)
        if "hub.docker.com/v2/search" in str(request.url):
            return httpx.Response(200, json={"results": []})
        return httpx.Response(404)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await find_alternatives("library/postgres", client=client)
    # No exception, empty list.
    assert result.candidates == []
