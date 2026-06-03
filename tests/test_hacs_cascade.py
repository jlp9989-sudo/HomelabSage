"""Tests for the HACS Python-bump cascade detector."""

from __future__ import annotations

import asyncio

from homelabsage import hacs_cascade as hc
from homelabsage.hacs_cascade import (
    PythonBumpFinding,
    detect_python_bump,
    extract_python_floor,
)

# ─── extractor ───────────────────────────────────────────────────────


def test_extract_python_floor_strict_form():
    text = (
        "# auto-generated\n"
        "python_requires>=3.12\n"
        "homeassistant==2026.6.0\n"
    )
    assert extract_python_floor(text) == "3.12"


def test_extract_python_floor_quoted_form():
    text = 'python_requires = ">=3.13,<3.14"\n'
    assert extract_python_floor(text) == "3.13"


def test_extract_python_floor_missing():
    assert extract_python_floor("no python here\n") is None


def test_extract_python_floor_case_insensitive():
    assert extract_python_floor("Python_Requires >= 3.13") == "3.13"


# ─── detector orchestrator ───────────────────────────────────────────


def _stub_fetch(mapping: dict[str, str | None]):
    async def fake(tag: str, *, timeout: float = 15.0):
        return mapping.get(tag)
    return fake


def test_detect_python_bump_returns_none_when_no_change(monkeypatch):
    body = "python_requires>=3.12\n"
    monkeypatch.setattr(hc, "_fetch_constraints",
                        _stub_fetch({"2026.5.0": body, "2026.6.0": body}))
    out = asyncio.run(detect_python_bump(
        current_version="2026.5.0", new_version="2026.6.0",
    ))
    assert out is None


def test_detect_python_bump_returns_finding_on_bump(monkeypatch):
    monkeypatch.setattr(hc, "_fetch_constraints", _stub_fetch({
        "2026.5.0": "python_requires>=3.12\n",
        "2026.6.0": "python_requires>=3.13\n",
    }))
    out = asyncio.run(detect_python_bump(
        current_version="2026.5.0", new_version="2026.6.0",
    ))
    assert out is not None
    assert isinstance(out, PythonBumpFinding)
    assert out.from_version == "3.12"
    assert out.to_version == "3.13"
    ctx = out.to_context()
    assert ctx["from"] == "3.12"
    assert ctx["to"] == "3.13"
    assert "2026.5.0" in ctx["old_constraints_url"]
    assert "2026.6.0" in ctx["new_constraints_url"]


def test_detect_python_bump_returns_none_when_fetch_fails(monkeypatch):
    monkeypatch.setattr(hc, "_fetch_constraints",
                        _stub_fetch({"a": None, "b": None}))
    out = asyncio.run(detect_python_bump(current_version="a", new_version="b"))
    assert out is None


def test_detect_python_bump_returns_none_when_same_version(monkeypatch):
    out = asyncio.run(detect_python_bump(
        current_version="2026.5.0", new_version="2026.5.0",
    ))
    assert out is None


def test_detect_python_bump_strips_v_prefix(monkeypatch):
    """`v2026.5.0` tags should resolve to the same URL as the unprefixed form."""
    captured: list[str] = []

    async def fake(tag: str, *, timeout: float = 15.0):
        captured.append(tag)
        return "python_requires>=3.12\n"

    monkeypatch.setattr(hc, "_fetch_constraints", fake)
    asyncio.run(detect_python_bump(
        current_version="v2026.5.0", new_version="v2026.5.0",
    ))
    # Even on same-version we still strip the prefix in the call
    # (the early exit happens before the fetch, so no fetch is observed
    # in this case — confirm by inspecting via a real bump).
    monkeypatch.setattr(hc, "_fetch_constraints", _stub_fetch({
        "2026.5.0": "python_requires>=3.12\n",
        "2026.6.0": "python_requires>=3.13\n",
    }))
    out = asyncio.run(detect_python_bump(
        current_version="v2026.5.0", new_version="v2026.6.0",
    ))
    assert out is not None
    assert out.from_version == "3.12"
