"""Unit tests for the curator.

These tests exercise the pure helpers and the on-disk state machine. The
Docker client and the LLM client are stubbed — no daemons or networks are
touched.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from homelabsage.config import CuratorConfig, DockerSourceConfig
from homelabsage.curator import (
    DEFAULT_PROMPT_TEMPLATE,
    PROMPT_PLACEHOLDERS,
    ContainerSnapshot,
    Curator,
    _existing_footer,
    _redact_env,
    _safe_filename,
    _strip_fences,
)

# ─── Pure helpers ────────────────────────────────────────────────────────


def test_redact_env_masks_secret_names():
    out = _redact_env(
        [
            "GITHUB_TOKEN=ghp_abc123",
            "API_KEY=sk-xyz",
            "PASSWORD=hunter2",
            "MY_SECRET=foo",
            "DATABASE_DSN=postgres://...",
        ]
    )
    for entry in out:
        name, _, value = entry.partition("=")
        assert value == "[REDACTED]", f"expected redacted, got {entry}"


def test_redact_env_keeps_innocent_vars():
    out = _redact_env(["TZ=Europe/Madrid", "PORT=8080", "FEATURE_X=on"])
    assert out == ["TZ=Europe/Madrid", "PORT=8080", "FEATURE_X=on"]


def test_redact_env_handles_entries_without_equals():
    # Some `Env` arrays from older daemons may contain plain strings.
    out = _redact_env(["MALFORMED"])
    assert out == ["MALFORMED"]


def test_safe_filename_strips_dangerous_chars():
    assert _safe_filename("homelabsage") == "homelabsage.md"
    assert _safe_filename("foo/bar") == "foo_bar.md"
    assert _safe_filename("../escape") == "escape.md"
    assert _safe_filename("with spaces!") == "with_spaces.md"


def test_safe_filename_falls_back_for_empty_input():
    assert _safe_filename("") == "container.md"
    assert _safe_filename("///") == "container.md"


def test_strip_fences_removes_markdown_wrapper():
    raw = "```markdown\n# Hello\nbody\n```"
    assert _strip_fences(raw) == "# Hello\nbody"


def test_strip_fences_removes_plain_triple_backticks():
    raw = "```\nplain body\n```"
    assert _strip_fences(raw) == "plain body"


def test_strip_fences_leaves_unfenced_text_alone():
    raw = "## Header\n\nbody"
    assert _strip_fences(raw) == "## Header\n\nbody"


def test_existing_footer_parses_marker():
    body = "## Note\n\nstuff\n<!-- curator: homelabsage@abc123def456 -->\n"
    assert _existing_footer(body) == ("homelabsage", "abc123def456")


def test_existing_footer_returns_none_when_absent():
    assert _existing_footer("## just a manual note\n") is None


def test_default_template_uses_all_documented_placeholders():
    for ph in PROMPT_PLACEHOLDERS:
        assert "{" + ph + "}" in DEFAULT_PROMPT_TEMPLATE, ph


# ─── Curator state machine ──────────────────────────────────────────────


def _curator(tmp_path: Path, **overrides) -> Curator:
    cfg = CuratorConfig(**overrides)
    return Curator(
        cfg=cfg,
        llm=_FakeLLM(),
        docker_cfg=DockerSourceConfig(enabled=True),
        notes_dir=str(tmp_path),
    )


def _snapshot(name: str = "homelabsage", digest: str = "abc123def456") -> ContainerSnapshot:
    return ContainerSnapshot(
        name=name,
        image="ghcr.io/owner/repo:0.1.0",
        image_digest_short=digest,
        repo="owner/repo",
        current_version="0.1.0",
        ports=["8000/tcp -> 0.0.0.0:8000"],
        mounts=["/host -> /app (rw)"],
        env_vars=["TZ=Europe/Madrid"],
        labels={"com.docker.compose.project": "homelabsage"},
    )


class _FakeLLM:
    def __init__(self, body: str = "## Purpose\n\nA single line.") -> None:
        self.body = body
        self.calls: list[str] = []

    def is_enabled(self) -> bool:  # pragma: no cover — trivial
        return True

    async def generate_text(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self.body


def test_existing_state_none_when_no_file(tmp_path: Path):
    curator = _curator(tmp_path)
    state, path = curator._existing_state(_snapshot())
    assert state == "none"
    assert path is not None
    assert not path.exists()


def test_existing_state_same_digest_skips(tmp_path: Path):
    curator = _curator(tmp_path)
    snap = _snapshot(digest="abc123def456")
    note_path = tmp_path / "homelabsage.md"
    note_path.write_text(
        "body\n<!-- curator: homelabsage@abc123def456 -->\n", encoding="utf-8"
    )
    state, _ = curator._existing_state(snap)
    assert state == "same_digest"


def test_existing_state_digest_changed_triggers_regen(tmp_path: Path):
    curator = _curator(tmp_path)
    snap = _snapshot(digest="new000000000")
    (tmp_path / "homelabsage.md").write_text(
        "body\n<!-- curator: homelabsage@oldoldoldold -->\n", encoding="utf-8"
    )
    state, _ = curator._existing_state(snap)
    assert state == "digest_changed"


def test_existing_state_manual_when_no_footer(tmp_path: Path):
    curator = _curator(tmp_path)
    snap = _snapshot()
    (tmp_path / "homelabsage.md").write_text("# my hand-written note\n", encoding="utf-8")
    state, _ = curator._existing_state(snap)
    assert state == "manual"


# ─── End-to-end behaviour ───────────────────────────────────────────────


def test_curate_one_writes_note_with_footer(tmp_path: Path):
    curator = _curator(tmp_path, recent_releases=0)
    snap = _snapshot(digest="deadbeef0000")
    result = asyncio.run(curator.curate_one(snap))
    assert result.status == "written"
    body = (tmp_path / "homelabsage.md").read_text(encoding="utf-8")
    assert "<!-- curator: homelabsage@deadbeef0000 -->" in body
    assert "## Purpose" in body


def test_curate_one_dry_run_does_not_touch_filesystem(tmp_path: Path):
    curator = _curator(tmp_path, recent_releases=0)
    snap = _snapshot()
    result = asyncio.run(curator.curate_one(snap, dry_run=True))
    assert result.status == "skipped_dry_run"
    assert result.body is not None
    assert "<!-- curator:" in result.body
    assert not (tmp_path / "homelabsage.md").exists()


def test_curate_one_skips_when_manual_note_present(tmp_path: Path):
    curator = _curator(tmp_path, recent_releases=0)
    (tmp_path / "homelabsage.md").write_text("hand-written, hands-off.\n", encoding="utf-8")
    snap = _snapshot()
    result = asyncio.run(curator.curate_one(snap))
    assert result.status == "skipped_manual"
    # File untouched.
    assert (
        (tmp_path / "homelabsage.md").read_text(encoding="utf-8")
        == "hand-written, hands-off.\n"
    )


def test_curate_one_force_overwrites_manual(tmp_path: Path):
    curator = _curator(tmp_path, recent_releases=0)
    (tmp_path / "homelabsage.md").write_text("manual\n", encoding="utf-8")
    snap = _snapshot(digest="forced0000000")
    result = asyncio.run(curator.curate_one(snap, force=True))
    assert result.status == "written"
    body = (tmp_path / "homelabsage.md").read_text(encoding="utf-8")
    assert "<!-- curator: homelabsage@forced0000000 -->" in body


def test_curate_one_regenerates_on_digest_change(tmp_path: Path):
    curator = _curator(tmp_path, recent_releases=0)
    (tmp_path / "homelabsage.md").write_text(
        "stale\n<!-- curator: homelabsage@oldoldoldold -->\n", encoding="utf-8"
    )
    snap = _snapshot(digest="newnewnewnew")
    result = asyncio.run(curator.curate_one(snap))
    assert result.status == "written"
    body = (tmp_path / "homelabsage.md").read_text(encoding="utf-8")
    assert "<!-- curator: homelabsage@newnewnewnew -->" in body
    assert "oldoldoldold" not in body


# ─── Prompt rendering ───────────────────────────────────────────────────


def test_build_prompt_substitutes_known_placeholders(tmp_path: Path):
    curator = _curator(tmp_path, recent_releases=0)
    snap = _snapshot()
    prompt = curator.build_prompt(snap, release_notes="(no releases)", style_examples="(none provided)")
    assert "homelabsage" in prompt
    assert "ghcr.io/owner/repo:0.1.0" in prompt
    assert "owner/repo" in prompt
    assert "0.1.0" in prompt
    # The literal placeholder text should NOT remain in the rendered prompt.
    for ph in ("{container_name}", "{image}", "{repo}", "{release_notes}"):
        assert ph not in prompt


def test_build_prompt_keeps_unknown_placeholders_intact(tmp_path: Path):
    curator = _curator(tmp_path, recent_releases=0)
    snap = _snapshot()
    custom_template = "Hello {container_name} — debug: {something_unknown}"
    prompt = curator.build_prompt(
        snap,
        release_notes="(no releases)",
        style_examples="(none)",
        template=custom_template,
    )
    assert "Hello homelabsage" in prompt
    assert "{something_unknown}" in prompt


def test_load_template_falls_back_when_path_missing(tmp_path: Path):
    curator = _curator(tmp_path, prompt_template_path=str(tmp_path / "does_not_exist.txt"))
    assert curator.load_template() == DEFAULT_PROMPT_TEMPLATE


def test_load_template_reads_configured_file(tmp_path: Path):
    custom = tmp_path / "custom.txt"
    custom.write_text("CUSTOM TEMPLATE for {container_name}", encoding="utf-8")
    curator = _curator(tmp_path, prompt_template_path=str(custom))
    assert curator.load_template().startswith("CUSTOM TEMPLATE")


def test_style_examples_loaded_when_files_exist(tmp_path: Path):
    example = tmp_path / "example.md"
    example.write_text("# Example body", encoding="utf-8")
    curator = _curator(tmp_path, style_examples=[str(example)])
    rendered = curator.load_style_examples()
    assert "example.md" in rendered
    assert "Example body" in rendered


def test_style_examples_empty_when_unconfigured(tmp_path: Path):
    curator = _curator(tmp_path)
    assert curator.load_style_examples() == "(none provided)"


# ─── Fence stripping happens before footer append ───────────────────────


def test_curate_one_strips_fences_from_llm_output(tmp_path: Path):
    curator = _curator(tmp_path, recent_releases=0)
    curator.llm = _FakeLLM(body="```markdown\n## Purpose\nA line.\n```")
    snap = _snapshot()
    result = asyncio.run(curator.curate_one(snap))
    assert result.status == "written"
    body = (tmp_path / "homelabsage.md").read_text(encoding="utf-8")
    assert "```" not in body
    assert "## Purpose" in body


def test_curate_one_returns_llm_failed_on_empty_response(tmp_path: Path):
    curator = _curator(tmp_path, recent_releases=0)
    curator.llm = _FakeLLM(body="")
    snap = _snapshot()
    result = asyncio.run(curator.curate_one(snap))
    assert result.status == "llm_failed"
    assert not (tmp_path / "homelabsage.md").exists()
