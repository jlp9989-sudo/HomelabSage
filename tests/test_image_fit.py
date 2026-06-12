"""Will-it-fit cross-signal: pure module + the disk_pressure helper it leans on."""

from __future__ import annotations

from pathlib import Path

from homelabsage.disk_pressure import tightest_free
from homelabsage.image_fit import evaluate

_MIB = 1024 * 1024


# ─── image_fit.evaluate ───────────────────────────────────────────


def test_wont_fit_when_image_larger_than_free():
    v = evaluate(new_image_bytes=600 * _MIB, free_bytes=400 * _MIB, path="/data")
    assert v is not None
    assert v.verdict == "wont_fit"
    assert v.severity == "critical"
    assert v.headroom_after_mib < 0     # negative: the pull overflows


def test_tight_when_fits_but_below_headroom():
    # 400 MiB image, 500 MiB free → fits, but 500 < 400*1.5=600 → tight.
    v = evaluate(new_image_bytes=400 * _MIB, free_bytes=500 * _MIB, path="/data")
    assert v is not None
    assert v.verdict == "tight"
    assert v.severity == "high"
    assert round(v.headroom_after_mib) == 100


def test_fits_returns_none():
    # 400 MiB image, 1000 MiB free → plenty of slack, no signal.
    assert evaluate(
        new_image_bytes=400 * _MIB, free_bytes=1000 * _MIB, path="/data",
    ) is None


def test_zero_or_unknown_inputs_return_none():
    assert evaluate(new_image_bytes=0, free_bytes=500 * _MIB, path="/d") is None
    assert evaluate(new_image_bytes=400 * _MIB, free_bytes=0, path="/d") is None


def test_headroom_ratio_floored_to_one():
    # ratio < 1 must not invent a "tight" zone below the image's own size:
    # with ratio=0.5 the tight branch collapses into wont_fit, so a 400 MiB
    # image with 500 MiB free (free >= image) is just "fits" → None.
    assert evaluate(
        new_image_bytes=400 * _MIB, free_bytes=500 * _MIB,
        path="/d", headroom_ratio=0.5,
    ) is None


def test_to_context_shape_and_rounding():
    v = evaluate(new_image_bytes=600 * _MIB, free_bytes=410 * _MIB, path="/mnt/cache")
    assert v is not None
    ctx = v.to_context()
    assert ctx == {
        "verdict": "wont_fit",
        "severity": "critical",
        "new_image_mib": 600.0,
        "free_mib": 410.0,
        "headroom_after_mib": -190.0,
        "path": "/mnt/cache",
    }


# ─── disk_pressure.tightest_free ──────────────────────────────────


def test_tightest_free_returns_free_and_path(tmp_path: Path):
    got = tightest_free([str(tmp_path)])
    assert got is not None
    free, path = got
    assert free > 0
    assert path == str(tmp_path)


def test_tightest_free_skips_unstatable_paths(tmp_path: Path):
    missing = str(tmp_path / "does-not-exist")
    assert tightest_free([missing]) is None
    # A real path alongside a missing one still resolves.
    got = tightest_free([missing, str(tmp_path)])
    assert got is not None and got[1] == str(tmp_path)


def test_tightest_free_dedups_same_device(tmp_path: Path):
    sub = tmp_path / "sub"
    sub.mkdir()
    # tmp_path and its subdir share a device → one result, not two.
    got = tightest_free([str(tmp_path), str(sub)])
    assert got is not None
