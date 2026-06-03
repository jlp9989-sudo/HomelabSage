"""Tests for the notes_dir git auto-commit helper."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from homelabsage.notes_git import auto_commit, commit_curator_write, is_git_repo


def _git_available() -> bool:
    return shutil.which("git") is not None


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@homelabsage.local"],
        cwd=path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"], cwd=path, check=True,
    )


# ─── is_git_repo ────────────────────────────────────────────────────


def test_is_git_repo_negative(tmp_path: Path):
    assert not is_git_repo(tmp_path)


def test_is_git_repo_positive(tmp_path: Path):
    if not _git_available():
        pytest.skip("git not on PATH")
    _init_repo(tmp_path)
    assert is_git_repo(tmp_path)


def test_is_git_repo_empty_string():
    assert not is_git_repo("")


# ─── auto_commit ────────────────────────────────────────────────────


def test_auto_commit_returns_false_when_not_a_repo(tmp_path: Path):
    f = tmp_path / "x.md"
    f.write_text("hi")
    assert not auto_commit(tmp_path, files=[f], message="t")


def test_auto_commit_creates_a_commit(tmp_path: Path):
    if not _git_available():
        pytest.skip("git not on PATH")
    _init_repo(tmp_path)
    f = tmp_path / "x.md"
    f.write_text("hi")
    ok = auto_commit(tmp_path, files=[f], message="add x")
    assert ok
    # The HEAD should reference the new commit
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=tmp_path,
        capture_output=True, text=True, check=True,
    )
    assert "add x" in log.stdout


def test_auto_commit_is_idempotent(tmp_path: Path):
    """Same body re-written → no second commit."""
    if not _git_available():
        pytest.skip("git not on PATH")
    _init_repo(tmp_path)
    f = tmp_path / "x.md"
    f.write_text("hi")
    auto_commit(tmp_path, files=[f], message="first")
    # Re-staging without a body change must NOT produce a new commit.
    f.write_text("hi")  # same content
    ok = auto_commit(tmp_path, files=[f], message="second")
    assert not ok
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=tmp_path,
        capture_output=True, text=True, check=True,
    )
    assert log.stdout.count("\n") == 1


def test_auto_commit_skips_files_outside_repo(tmp_path: Path):
    """File outside the notes_dir is silently dropped from the stage."""
    if not _git_available():
        pytest.skip("git not on PATH")
    _init_repo(tmp_path)
    inside = tmp_path / "x.md"
    inside.write_text("hi")
    outside = tmp_path.parent / "other" / "y.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("nope")
    ok = auto_commit(tmp_path, files=[outside, inside], message="x only")
    assert ok
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=tmp_path,
        capture_output=True, text=True, check=True,
    )
    assert "x only" in log.stdout


# ─── commit_curator_write ───────────────────────────────────────────


def test_commit_curator_write_message_format(tmp_path: Path):
    if not _git_available():
        pytest.skip("git not on PATH")
    _init_repo(tmp_path)
    f = tmp_path / "mealie.md"
    f.write_text("a note about mealie")
    ok = commit_curator_write(
        tmp_path,
        target_kind="container",
        target_name="mealie",
        update_id="docker:mealie:2.0",
        paths=[f],
    )
    assert ok
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=tmp_path,
        capture_output=True, text=True, check=True,
    )
    assert "curator: container=mealie" in log.stdout
    assert "update=docker:mealie:2.0" in log.stdout


def test_commit_curator_write_no_update_id(tmp_path: Path):
    if not _git_available():
        pytest.skip("git not on PATH")
    _init_repo(tmp_path)
    f = tmp_path / "system.md"
    f.write_text("kernel: foo")
    ok = commit_curator_write(
        tmp_path, target_kind="system", target_name="host", paths=[f],
    )
    assert ok
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=tmp_path,
        capture_output=True, text=True, check=True,
    )
    assert "curator: system=host" in log.stdout
    assert "update=" not in log.stdout
