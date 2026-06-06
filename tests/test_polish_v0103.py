"""Tests for v0.10.3: punch-list I4 (post-LLM redact) + I5 (stale notion_page_id)."""

from __future__ import annotations

import asyncio

import httpx

from homelabsage.config import NotionOutputConfig
from homelabsage.db import Database
from homelabsage.models import Analysis, AnalyzedUpdate, Severity, Update
from homelabsage.outputs.notion import NotionOutput


def _make_item(summary: str, page_id: str | None = None):
    return AnalyzedUpdate(
        update=Update(source="docker", subject="mealie",
                      current_version="1.0", new_version="2.0"),
        analysis=Analysis(severity=Severity.HIGH, summary=summary),
        notion_page_id=page_id,
    )


# ─── I4: post-LLM redact of analysis.summary into Notion ──────────


def _capture_payloads(monkeypatch, responses):
    """Patch httpx.AsyncClient to capture each request body."""
    captured: list[dict] = []
    resp_iter = iter(responses)

    class _MockResponse:
        def __init__(self, status_code: int, body: dict | None = None):
            self.status_code = status_code
            self._body = body or {}
        def json(self): return self._body
        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"err {self.status_code}",
                    request=httpx.Request("POST", "https://api.notion.com/v1/pages"),
                    response=httpx.Response(self.status_code),
                )

    class _MockClient:
        def __init__(self, *_a, **_kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
        async def patch(self, url, **kw):
            captured.append({"method": "PATCH", "url": url, "json": kw.get("json")})
            return next(resp_iter)
        async def post(self, url, **kw):
            captured.append({"method": "POST", "url": url, "json": kw.get("json")})
            return next(resp_iter)

    monkeypatch.setattr(httpx, "AsyncClient", _MockClient)
    return captured


def test_notion_summary_is_redacted_before_send(monkeypatch, tmp_path):
    """A summary the LLM hallucinated with a `ghp_…` token in it must
    have the token redacted before Notion sees it."""
    cfg = NotionOutputConfig(enabled=True, api_key="x", database_id="db")
    db = Database(tmp_path / "t.db")
    out = NotionOutput(cfg, db=db)

    leaky_summary = "Update OK. GITHUB_TOKEN=ghp_AbCdEfGhIjKlMnOpQrStUvWxYzAbCdEfGh notice"
    item = _make_item(summary=leaky_summary)
    db.upsert(item)

    class _OK:
        status_code = 200
        def json(self): return {"id": "page-123"}
        def raise_for_status(self): return None

    captured = _capture_payloads(monkeypatch, [_OK()])
    asyncio.run(out.send(item))

    assert len(captured) == 1
    sent_summary = (
        captured[0]["json"]["properties"]["Summary"]
        ["rich_text"][0]["text"]["content"]
    )
    assert "ghp_AbCdEfGhIjKlMnOpQrStUvWxYzAbCdEfGh" not in sent_summary
    # Either the whole secret line was stripped, or only the value swapped
    # to the redaction placeholder.
    assert "redacted" in sent_summary.lower()


def test_notion_clean_summary_passes_through(monkeypatch, tmp_path):
    """Defensive: a clean summary is NOT touched by the redactor."""
    cfg = NotionOutputConfig(enabled=True, api_key="x", database_id="db")
    db = Database(tmp_path / "t.db")
    out = NotionOutput(cfg, db=db)

    item = _make_item(summary="Patch fixes CVE-2026-1234 in libxml2 — apply when convenient")
    db.upsert(item)

    class _OK:
        status_code = 200
        def json(self): return {"id": "p1"}
        def raise_for_status(self): return None

    captured = _capture_payloads(monkeypatch, [_OK()])
    asyncio.run(out.send(item))
    sent = (
        captured[0]["json"]["properties"]["Summary"]
        ["rich_text"][0]["text"]["content"]
    )
    assert "CVE-2026-1234" in sent
    assert "libxml2" in sent


# ─── I5: 404 on PATCH clears page_id + POSTs a fresh page ─────────


def test_notion_404_on_patch_clears_page_id_and_creates_fresh(monkeypatch, tmp_path):
    """When the cached page_id 404s on PATCH, the output:
      - sets `item.notion_page_id = None`
      - persists None to DB (so next scan starts fresh)
      - POSTs a new page in the same call
      - persists the fresh page id
    """
    cfg = NotionOutputConfig(enabled=True, api_key="x", database_id="db")
    db = Database(tmp_path / "t.db")
    out = NotionOutput(cfg, db=db)

    item = _make_item(summary="stable", page_id="stale-deleted-id")
    db.upsert(item)
    db.set_notion_page_id(item.id, "stale-deleted-id")

    class _NotFound:
        status_code = 404
        def json(self): return {}
        def raise_for_status(self): raise httpx.HTTPStatusError(
            "404", request=httpx.Request("PATCH", "https://api.notion.com/v1/pages/stale"),
            response=httpx.Response(404),
        )

    class _CreatedFresh:
        status_code = 200
        def json(self): return {"id": "new-page-after-404"}
        def raise_for_status(self): return None

    captured = _capture_payloads(monkeypatch, [_NotFound(), _CreatedFresh()])
    asyncio.run(out.send(item))

    # Two HTTP calls: PATCH (404) then POST (created)
    assert [c["method"] for c in captured] == ["PATCH", "POST"]
    # In-memory id updated to new
    assert item.notion_page_id == "new-page-after-404"
    # DB persisted the new id (overwriting the stale one)
    row = db._conn.execute(
        "SELECT notion_page_id FROM updates WHERE id = ?", (item.id,),
    ).fetchone()
    assert row["notion_page_id"] == "new-page-after-404"


def test_notion_non_404_patch_failure_still_logs(monkeypatch, tmp_path):
    """A 500 on PATCH should NOT re-POST (might create duplicate); it just
    logs and leaves the page_id intact for retry next scan."""
    cfg = NotionOutputConfig(enabled=True, api_key="x", database_id="db")
    db = Database(tmp_path / "t.db")
    out = NotionOutput(cfg, db=db)

    item = _make_item(summary="x", page_id="still-valid-id")
    db.upsert(item)
    db.set_notion_page_id(item.id, "still-valid-id")

    class _ServerErr:
        status_code = 503
        def json(self): return {}
        def raise_for_status(self): raise httpx.HTTPStatusError(
            "503", request=httpx.Request("PATCH", "https://api.notion.com/v1/pages/x"),
            response=httpx.Response(503),
        )

    captured = _capture_payloads(monkeypatch, [_ServerErr()])
    asyncio.run(out.send(item))

    # Only PATCH; no POST follow-up
    assert [c["method"] for c in captured] == ["PATCH"]
    # page_id untouched — Notion was just down, not deleted
    assert item.notion_page_id == "still-valid-id"


def test_db_set_notion_page_id_accepts_none(tmp_path):
    """Sanity: the DB layer accepts None to clear the cached id."""
    db = Database(tmp_path / "t.db")
    item = _make_item(summary="x", page_id="abc")
    db.upsert(item)
    db.set_notion_page_id(item.id, "abc")
    db.set_notion_page_id(item.id, None)
    row = db._conn.execute(
        "SELECT notion_page_id FROM updates WHERE id = ?", (item.id,),
    ).fetchone()
    assert row["notion_page_id"] is None
