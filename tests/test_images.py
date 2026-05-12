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
    _hub_repo_to_alt,
    _hub_search_row_to_partial,
    _jaccard,
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

def test_hub_search_row_partial_basic():
    """Search rows have all fields; partial preserves them for stage 2."""
    p = _hub_search_row_to_partial(
        {
            "repo_name": "linuxserver/jellyfin",
            "short_description": "Free Software Media System",
            "pull_count": 50_000_000,
            "last_updated": "2026-05-01T10:00:00.000000Z",
        }
    )
    assert p is not None
    assert p["name"] == "linuxserver/jellyfin"
    assert p["pulls"] == 50_000_000
    assert p["last_updated"] == "2026-05-01T10:00:00.000000Z"


def test_hub_search_row_partial_handles_missing_last_updated():
    """Docker Hub search often omits last_updated — we keep the row anyway
    and defer the timestamp to the per-repo enrichment step."""
    p = _hub_search_row_to_partial(
        {"repo_name": "hkotel/mealie", "pull_count": 15_000_000}
    )
    assert p is not None
    assert p["last_updated"] is None


def test_hub_search_row_partial_implicit_library_namespace():
    p = _hub_search_row_to_partial(
        {"name": "postgres", "pull_count": 1_000_000_000}
    )
    assert p is not None
    assert p["name"] == "library/postgres"


def test_hub_search_row_partial_returns_none_without_name():
    assert _hub_search_row_to_partial({"pull_count": 1}) is None


def test_hub_repo_to_alt_builds_from_full_metadata():
    """The per-repo endpoint returns a different shape (description, not
    short_description; the timestamp is always present)."""
    alt = _hub_repo_to_alt(
        "hkotel/mealie",
        {
            "pull_count": 15_416_416,
            "last_updated": "2026-05-11T21:16:17.305353Z",
            "description": "A Self Hosted Recipe Manager",
        },
    )
    assert alt is not None
    assert alt.image == "hkotel/mealie"
    assert alt.pulls == 15_416_416
    assert alt.source == "docker-hub"
    assert "Recipe Manager" in alt.short_description


def test_hub_repo_to_alt_marks_lsio_source():
    """The same builder serves the LSIO direct probe — only the source tag differs."""
    alt = _hub_repo_to_alt(
        "linuxserver/jellyfin",
        {"pull_count": 80_000_000, "last_updated": "2026-05-01T10:00:00Z"},
        source="lsio",
    )
    assert alt is not None
    assert alt.source == "lsio"
    assert alt.image == "linuxserver/jellyfin"


def test_hub_repo_to_alt_returns_none_without_timestamp():
    assert _hub_repo_to_alt("x/y", {"pull_count": 1}) is None


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

def _build_mock_transport(
    *,
    hub_rows: list[dict],
    repos_by_name: dict[str, dict] | None = None,
    current_pulls: int | None = None,
    tags_by_repo: dict[str, list[str]] | None = None,
) -> httpx.MockTransport:
    """Build a transport that emulates Docker Hub's three relevant endpoints.

    `hub_rows`      — what the search endpoint returns
    `repos_by_name` — per-repo metadata for enrichment + LSIO direct probe
    `current_pulls` — pull count for the USER's image (`/v2/repositories/<user>/`)
    `tags_by_repo`  — tag list per repo for the tag-enrichment step
    """
    repos_by_name = repos_by_name or {}
    tags_by_repo = tags_by_repo or {}

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        if host == "hub.docker.com" and path.startswith("/v2/search/repositories"):
            return httpx.Response(200, json={"results": hub_rows})
        if host == "hub.docker.com" and path.startswith("/v2/repositories/"):
            tail = path[len("/v2/repositories/"):]
            if tail.endswith("/tags/"):
                repo = tail[: -len("/tags/")]
                return httpx.Response(200, json={
                    "results": [{"name": t} for t in tags_by_repo.get(repo, [])]
                })
            repo = tail.rstrip("/")
            # `repos_by_name` overrides any per-call data; otherwise fall back
            # to `current_pulls` for the user-image lookup path.
            if repo in repos_by_name:
                return httpx.Response(200, json=repos_by_name[repo])
            if current_pulls is not None:
                return httpx.Response(200, json={"pull_count": current_pulls})
            return httpx.Response(404)
        return httpx.Response(404, text="unhandled in mock")

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_find_alternatives_returns_high_value_only():
    """End-to-end against a realistic synthetic catalogue.

    The search endpoint deliberately omits `last_updated` on most rows (mirrors
    what Hub actually does in production). The orchestrator must fall back to
    the per-repo endpoint for the top-N to fill those in.
    """
    now = datetime.now(UTC)
    recent = (now - timedelta(days=10)).isoformat().replace("+00:00", "Z")
    stale = (now - timedelta(days=300)).isoformat().replace("+00:00", "Z")

    hub_rows = [
        # Search rows — no last_updated, force enrichment.
        {"repo_name": "hkotel/jellyfin", "short_description": "Jellyfin", "pull_count": 50_000_000},
        {"repo_name": "abandoned/jellyfin-fork", "short_description": "Old fork", "pull_count": 60_000_000},
        {"repo_name": "smallfork/jellyfin-mini", "short_description": "Personal fork of jellyfin media", "pull_count": 5_000},
        {"repo_name": "library/nginx", "short_description": "Web server", "pull_count": 9_000_000_000},
    ]
    repos_by_name = {
        "hkotel/jellyfin": {
            "pull_count": 50_000_000,
            "last_updated": recent,
            "description": "Jellyfin media server",
        },
        "abandoned/jellyfin-fork": {
            "pull_count": 60_000_000,
            "last_updated": stale,
            "description": "Old fork",
        },
        "smallfork/jellyfin-mini": {
            "pull_count": 5_000,
            "last_updated": recent,
            "description": "Personal fork of jellyfin media",
        },
        "library/nginx": {
            "pull_count": 9_000_000_000,
            "last_updated": recent,
            "description": "Web server",
        },
        # LSIO direct probe — must be present so the LSIO source contributes.
        "linuxserver/jellyfin": {
            "pull_count": 80_000_000,
            "last_updated": recent,
            "description": "Jellyfin media server",
        },
    }
    tags_by_repo = {
        "hkotel/jellyfin": ["latest", "10.10.3", "10.10.2"],
        "linuxserver/jellyfin": ["latest", "10.10.3"],
    }

    transport = _build_mock_transport(
        hub_rows=hub_rows,
        repos_by_name=repos_by_name,
        tags_by_repo=tags_by_repo,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        result = await find_alternatives(
            "ghcr.io/imagegenius/jellyfin",
            description="Jellyfin media server",
            client=client,
        )

    names = [a.image for a in result.candidates]
    sources = [a.source for a in result.candidates]
    assert "library/nginx" not in names              # unrelated → rejected
    assert "abandoned/jellyfin-fork" not in names    # stale → rejected
    assert "smallfork/jellyfin-mini" not in names    # too small → rejected
    # The popular & recent ones survive — the Hub entry and the LSIO direct entry.
    assert "hkotel/jellyfin" in names or "linuxserver/jellyfin" in names
    assert "lsio" in sources    # the linuxserver/ direct probe fired
    # The sample_tag enrichment must run on at least one survivor.
    assert any(a.sample_tag == "10.10.3" for a in result.candidates)
    assert result.sources_consulted == ["docker-hub", "lsio"]


@pytest.mark.asyncio
async def test_find_alternatives_returns_empty_when_no_candidates_pass():
    transport = _build_mock_transport(hub_rows=[], repos_by_name={})
    async with httpx.AsyncClient(transport=transport) as client:
        result = await find_alternatives("ghcr.io/x/y", client=client)
    assert result.candidates == []
    assert result.sources_consulted == ["docker-hub", "lsio"]


@pytest.mark.asyncio
async def test_find_alternatives_swallows_http_errors():
    """If the search endpoint fails, we still complete (LSIO probe may also
    fail; either way we return an empty list rather than crash)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v2/search"):
            return httpx.Response(500)
        return httpx.Response(404)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await find_alternatives("library/postgres", client=client)
    assert result.candidates == []
