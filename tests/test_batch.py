"""Tests for severity-aware notification batching."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from homelabsage.config import (
    BatchingConfig,
    Config,
    OutputsConfig,
    TelegramOutputConfig,
)
from homelabsage.models import (
    Analysis,
    AnalyzedUpdate,
    Severity,
    Update,
    UpdateStatus,
)
from homelabsage.outputs.batch import (
    below_threshold,
    dispatch_batch,
    render_batch,
)


def _make(sev: Severity, subject: str = "x") -> AnalyzedUpdate:
    return AnalyzedUpdate(
        update=Update(source="docker", subject=subject,
                      current_version="1", new_version="2"),
        analysis=Analysis(severity=sev, summary=f"{subject} summary"),
        status=UpdateStatus.ANALYZED,
        detected_at=datetime(2026, 6, 1, 12, 0, 0),
    )


# ─── below_threshold ──────────────────────────────────────────────────


def test_below_threshold_low_severity_returns_true():
    assert below_threshold(_make(Severity.INFO), Severity.HIGH) is True
    assert below_threshold(_make(Severity.MEDIUM), Severity.HIGH) is True


def test_below_threshold_at_or_above_threshold_returns_false():
    assert below_threshold(_make(Severity.HIGH), Severity.HIGH) is False
    assert below_threshold(_make(Severity.CRITICAL), Severity.HIGH) is False


def test_below_threshold_no_analysis_returns_false():
    item = _make(Severity.INFO)
    item.analysis = None
    assert below_threshold(item, Severity.HIGH) is False


# ─── render ───────────────────────────────────────────────────────────


def test_render_batch_includes_count_and_per_item_summary():
    items = [_make(Severity.INFO, "a"), _make(Severity.MEDIUM, "b")]
    body = render_batch(items)
    assert "2 low-severity updates" in body
    assert "a" in body and "b" in body
    assert "info" in body and "medium" in body


def test_render_batch_truncates_at_25():
    items = [_make(Severity.INFO, f"svc{i}") for i in range(40)]
    body = render_batch(items)
    assert "40 low-severity" in body
    assert "truncated" in body
    # 25 listed + the count line — services beyond 25 are dropped
    assert "svc24" in body
    assert "svc25" not in body


# ─── dispatch ─────────────────────────────────────────────────────────


class _Recorder:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        pass
    async def post(self, url, **kw):
        self.calls.append({"url": url, **kw})
        class _R:
            status_code = 200
            def raise_for_status(self): pass
        return _R()


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: rec)
    return rec


async def test_dispatch_batch_no_op_on_empty_items(recorder):
    cfg = Config(outputs=OutputsConfig(
        telegram=TelegramOutputConfig(enabled=True, bot_token="b", chat_id="c"),
    ))
    res = await dispatch_batch(cfg, [])
    assert res == {}
    assert recorder.calls == []


async def test_dispatch_batch_hits_only_enabled_channels(recorder):
    # min_severity="info" set explicitly: since v0.12.2 the rollup honours
    # each channel's severity floor (telegram defaults to "high", which
    # would rightly filter this INFO item out — that's tested separately).
    cfg = Config(outputs=OutputsConfig(
        telegram=TelegramOutputConfig(
            enabled=True, bot_token="b", chat_id="c", min_severity="info",
        ),
    ))
    res = await dispatch_batch(cfg, [_make(Severity.INFO, "a")])
    assert res == {"telegram": "sent"}
    assert len(recorder.calls) == 1
    assert "telegram" in recorder.calls[0]["url"]


async def test_dispatch_batch_respects_channel_min_severity(recorder):
    """A channel whose floor is above every batched item gets skipped —
    the rollup must not smuggle low items past the per-channel gate."""
    cfg = Config(outputs=OutputsConfig(
        telegram=TelegramOutputConfig(enabled=True, bot_token="b", chat_id="c"),
    ))   # telegram default floor is "high"
    res = await dispatch_batch(cfg, [_make(Severity.INFO, "a")])
    assert res == {"telegram": "skipped: all items below min_severity"}
    assert recorder.calls == []


# ─── engine integration ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_engine_batches_low_severity_when_threshold_crossed(tmp_path, monkeypatch):
    from homelabsage.db import Database
    from homelabsage.engine import Engine
    from homelabsage.outputs import Output

    cfg = Config(
        outputs=OutputsConfig(
            batching=BatchingConfig(enabled=True, below_severity="high", min_count=2),
        ),
    )
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    cfg.llm.provider = "ollama"
    cfg.llm.endpoint = "http://stub"

    sent: list[tuple[str, str]] = []
    batched_calls: list[dict] = []

    class _Push(Output):
        id = "push"
        is_push = True
        async def send(self, item):
            a = item.analysis
            sent.append((item.update.subject, a.severity.value if a else "—"))

    class _Plugin:
        id = "p"
        _emitted = False
        async def scan(self):
            if self._emitted:
                return []
            self._emitted = True
            return [
                Update(source="p", subject="info1", current_version="1", new_version="2"),
                Update(source="p", subject="info2", current_version="1", new_version="2"),
                Update(source="p", subject="critical_one", current_version="1", new_version="2"),
            ]

    # Match severity to subject so the test reads naturally.
    async def fake_analyze(self, update, notes=""):
        from homelabsage.llm import LastCall
        self.last_call = LastCall("p", "{}", 0, 0, True, 1, True)
        sev_by_subj = {
            "info1": Severity.INFO,
            "info2": Severity.MEDIUM,
            "critical_one": Severity.CRITICAL,
        }
        return Analysis(severity=sev_by_subj[update.subject], summary="—")

    monkeypatch.setattr("homelabsage.llm.LLMClient.analyze", fake_analyze)

    async def fake_dispatch_batch(cfg_arg, items):
        batched_calls.append({"count": len(items), "subjects": [it.update.subject for it in items]})
        return {"telegram": "sent"}

    monkeypatch.setattr("homelabsage.engine.dispatch_batch", fake_dispatch_batch)

    db = Database(cfg.storage.database_path)
    engine = Engine(cfg, db)
    engine.plugins = [_Plugin()]  # type: ignore[list-item]
    engine.outputs = [_Push()]   # type: ignore[list-item]
    try:
        await engine.run_once()
    finally:
        engine.close()

    # Critical fired per-item; info+medium got batched.
    assert ("critical_one", "critical") in sent
    assert ("info1", "info") not in sent          # batched
    assert ("info2", "medium") not in sent        # batched
    assert len(batched_calls) == 1
    assert sorted(batched_calls[0]["subjects"]) == ["info1", "info2"]


@pytest.mark.asyncio
async def test_engine_skips_batch_below_min_count(tmp_path, monkeypatch):
    """1 low-sev item < min_count=2 → no batch dispatch, no per-item dispatch either."""
    from homelabsage.db import Database
    from homelabsage.engine import Engine
    from homelabsage.outputs import Output

    cfg = Config(
        outputs=OutputsConfig(
            batching=BatchingConfig(enabled=True, below_severity="high", min_count=2),
        ),
    )
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    cfg.llm.provider = "ollama"
    cfg.llm.endpoint = "http://stub"

    sent: list[str] = []
    batched_calls: list[dict] = []

    class _Push(Output):
        id = "push"
        is_push = True
        async def send(self, item):
            sent.append(item.update.subject)

    class _Plugin:
        id = "p"
        _emitted = False
        async def scan(self):
            if self._emitted:
                return []
            self._emitted = True
            return [Update(source="p", subject="only", current_version="1", new_version="2")]

    async def fake_analyze(self, update, notes=""):
        from homelabsage.llm import LastCall
        self.last_call = LastCall("p", "{}", 0, 0, True, 1, True)
        return Analysis(severity=Severity.INFO, summary="—")

    monkeypatch.setattr("homelabsage.llm.LLMClient.analyze", fake_analyze)
    monkeypatch.setattr(
        "homelabsage.engine.dispatch_batch",
        lambda *a, **kw: batched_calls.append({"called": True}) or {},
    )

    db = Database(cfg.storage.database_path)
    engine = Engine(cfg, db)
    engine.plugins = [_Plugin()]  # type: ignore[list-item]
    engine.outputs = [_Push()]   # type: ignore[list-item]
    try:
        await engine.run_once()
    finally:
        engine.close()

    assert sent == []            # batched (deferred)
    assert batched_calls == []   # below min_count → no rollup either
