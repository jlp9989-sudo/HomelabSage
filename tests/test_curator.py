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
    is_purpose_fallback,
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


# ─── is_purpose_fallback ─────────────────────────────────────────────────


def test_fallback_detected_for_exact_rule7_string():
    assert is_purpose_fallback("(no purpose stated yet — fill in)")


def test_fallback_detected_when_followed_by_speculative_bullets():
    """Gemini sometimes ignores the 'stop' instruction and adds bullets after
    the fallback. The lead is still the fallback, so the whole note is junk
    and we should treat it as the trigger."""
    body = (
        "(no purpose stated yet — fill in)\n"
        "\n"
        "- runs on port 8000\n"
        "- environment: TZ=Europe/Madrid\n"
    )
    assert is_purpose_fallback(body)


def test_fallback_detected_with_normal_dash_normalisation():
    assert is_purpose_fallback("(no purpose stated yet - fill in)")
    assert is_purpose_fallback("(no purpose stated yet – fill in)")  # en-dash


def test_fallback_detected_without_parens():
    assert is_purpose_fallback("no purpose stated yet — fill in")


def test_fallback_detected_when_wrapped_in_quotes_or_backticks():
    assert is_purpose_fallback("`(no purpose stated yet — fill in)`")
    assert is_purpose_fallback('"(no purpose stated yet — fill in)"')


def test_fallback_skips_blank_leading_lines():
    body = "\n\n   \n(no purpose stated yet — fill in)\n"
    assert is_purpose_fallback(body)


def test_fallback_not_detected_when_purpose_is_real():
    body = (
        "OpenClaw is the personal Telegram bot Hermes.\n\n"
        "- entrypoint: bot.py\n"
        "- volume: /opt/openclaw\n"
    )
    assert not is_purpose_fallback(body)


def test_fallback_not_detected_when_phrase_appears_as_aside():
    """If the fallback wording is buried in a real note, it does NOT count.
    Only the lead matters — that's what Rule 7 mandates."""
    body = (
        "OpenClaw is the personal Telegram bot Hermes.\n\n"
        "- previous run logged: '(no purpose stated yet — fill in)' in notes\n"
    )
    assert not is_purpose_fallback(body)


def test_fallback_not_detected_for_empty_or_whitespace_only():
    assert not is_purpose_fallback("")
    assert not is_purpose_fallback("   \n\n\t\n")


# ─── Interview integration ──────────────────────────────────────────────


def _curator_with_db(tmp_path: Path, llm: _FakeLLM):
    from homelabsage.db import Database

    db = Database(tmp_path / "state.sqlite")
    cfg = CuratorConfig()
    return Database, db, Curator(
        cfg=cfg,
        llm=llm,
        docker_cfg=DockerSourceConfig(enabled=True),
        notes_dir=str(tmp_path),
        db=db,
    )


def test_curate_one_persists_interview_question_on_fallback(tmp_path: Path):
    from homelabsage.models import InterviewStatus

    llm = _FakeLLM("(no purpose stated yet — fill in)")
    _Database, db, c = _curator_with_db(tmp_path, llm)
    snap = _snapshot()

    result = asyncio.run(c.curate_one(snap))

    assert result.status == "interview_pending"
    assert result.interview_question_id is not None
    # No note file written
    assert result.path is not None
    assert not result.path.exists()

    pending = db.list_interview_questions()
    assert len(pending) == 1
    assert pending[0].container_name == snap.name
    assert pending[0].image_digest_short == snap.image_digest_short
    assert pending[0].status is InterviewStatus.PENDING


def test_curate_one_repeated_fallback_does_not_duplicate_questions(tmp_path: Path):
    """Cron runs daily — same fallback shouldn't pile up rows."""
    llm = _FakeLLM("(no purpose stated yet — fill in)")
    _Database, db, c = _curator_with_db(tmp_path, llm)
    snap = _snapshot()

    first = asyncio.run(c.curate_one(snap))
    second = asyncio.run(c.curate_one(snap, force=True))

    assert first.interview_question_id == second.interview_question_id
    assert len(db.list_interview_questions()) == 1


def test_curate_one_with_user_purpose_skips_fallback_path(tmp_path: Path):
    """When a user has answered, re-curate must inject the purpose into the
    prompt and write the note, even if the (mocked) LLM still returns the
    fallback string. The presence of `user_purpose` short-circuits the
    interview detection by design — the user already told us the purpose."""
    llm = _FakeLLM("OpenClaw is the Hermes Telegram bot.\n\n- entrypoint: bot.py\n")
    _Database, db, c = _curator_with_db(tmp_path, llm)
    snap = _snapshot()

    result = asyncio.run(c.curate_one(snap, user_purpose="My personal Telegram bot."))

    assert result.status == "written"
    assert result.path is not None
    assert result.path.exists()
    assert "OpenClaw is the Hermes Telegram bot." in result.body
    # The fake LLM recorded the prompt — verify the purpose got injected
    assert any("My personal Telegram bot." in p for p in llm.calls)


def test_build_prompt_renders_user_purpose_block(tmp_path: Path):
    cfg = CuratorConfig()
    c = Curator(
        cfg=cfg,
        llm=_FakeLLM(),
        docker_cfg=DockerSourceConfig(enabled=True),
        notes_dir=str(tmp_path),
    )
    snap = _snapshot()
    prompt_with = c.build_prompt(snap, "(none)", "(none)", user_purpose="Hello world.")
    prompt_without = c.build_prompt(snap, "(none)", "(none)")

    assert "Hello world." in prompt_with
    assert "(none provided)" in prompt_without
    # The block header must be present so Rule 7's exception clause makes sense
    assert "User-provided purpose" in prompt_with
    assert "User-provided purpose" in prompt_without


def test_curate_one_without_db_still_works_on_fallback(tmp_path: Path):
    """No DB configured (e.g. ad-hoc CLI run) → still returns interview_pending
    but without persisting. Behaviour gracefully degrades."""
    llm = _FakeLLM("(no purpose stated yet — fill in)")
    cfg = CuratorConfig()
    c = Curator(
        cfg=cfg,
        llm=llm,
        docker_cfg=DockerSourceConfig(enabled=True),
        notes_dir=str(tmp_path),
        db=None,
    )
    result = asyncio.run(c.curate_one(_snapshot()))

    assert result.status == "interview_pending"
    assert result.interview_question_id is None


# ─── Suggestion generation ──────────────────────────────────────────────


class _ScriptedLLM:
    """LLM stub that returns a different body for each call in order.

    Used to differentiate the curator's main prompt response from the
    follow-up suggestion prompt — the curator calls generate_text twice
    when a Rule 7 fallback fires (once for the note, once for the guess).
    """

    def __init__(self, bodies: list[str]) -> None:
        self.bodies = list(bodies)
        self.calls: list[str] = []

    def is_enabled(self) -> bool:
        return True

    async def generate_text(self, prompt: str) -> str:
        self.calls.append(prompt)
        if not self.bodies:
            return ""
        return self.bodies.pop(0)


def test_curate_one_persists_suggestion_with_question(tmp_path: Path):
    from homelabsage.db import Database

    llm = _ScriptedLLM(
        [
            "(no purpose stated yet — fill in)",  # main curate prompt
            "Likely a Telegram bot framework wrapping LangChain.",  # suggestion
        ]
    )
    db = Database(tmp_path / "state.sqlite")
    c = Curator(
        cfg=CuratorConfig(),
        llm=llm,
        docker_cfg=DockerSourceConfig(enabled=True),
        notes_dir=str(tmp_path),
        db=db,
    )

    result = asyncio.run(c.curate_one(_snapshot()))

    assert result.status == "interview_pending"
    pending = db.list_interview_questions()
    assert len(pending) == 1
    assert pending[0].suggested_text == (
        "Likely a Telegram bot framework wrapping LangChain."
    )
    # Suggestion prompt must NOT include the heavy Rule 7 instructions.
    assert any("(no guess)" in p for p in llm.calls)


def test_suggestion_skipped_when_question_already_pending(tmp_path: Path):
    """Cron re-runs against the same image must not waste an LLM call
    regenerating the suggestion the user has already seen."""
    from homelabsage.db import Database

    # Only ONE response — the suggestion call would consume a second.
    # If the optimisation breaks, .pop on an empty list returns "" and the
    # test still passes; the meaningful check is the call count below.
    llm = _ScriptedLLM(
        ["(no purpose stated yet — fill in)"] * 4  # 2 curate + 2 suggestion if buggy
    )
    db = Database(tmp_path / "state.sqlite")
    c = Curator(
        cfg=CuratorConfig(),
        llm=llm,
        docker_cfg=DockerSourceConfig(enabled=True),
        notes_dir=str(tmp_path),
        db=db,
    )

    # First call: 1 curate + 1 suggestion = 2 LLM calls
    asyncio.run(c.curate_one(_snapshot()))
    after_first = len(llm.calls)

    # Second call (force=True, same digest): 1 curate + 0 suggestion = 1 call
    asyncio.run(c.curate_one(_snapshot(), force=True))
    after_second = len(llm.calls)

    assert after_first == 2
    assert after_second - after_first == 1, (
        f"expected exactly one new call (curate only), got {after_second - after_first}"
    )


def test_suggestion_decline_marker_yields_none(tmp_path: Path):
    from homelabsage.db import Database

    llm = _ScriptedLLM(
        ["(no purpose stated yet — fill in)", "(no guess)"]
    )
    db = Database(tmp_path / "state.sqlite")
    c = Curator(
        cfg=CuratorConfig(),
        llm=llm,
        docker_cfg=DockerSourceConfig(enabled=True),
        notes_dir=str(tmp_path),
        db=db,
    )

    asyncio.run(c.curate_one(_snapshot()))
    pending = db.list_interview_questions()
    assert pending[0].suggested_text is None


def test_suggestion_fallback_loop_yields_none(tmp_path: Path):
    """If the suggestion model also emits the Rule 7 fallback wording, we
    treat it as 'no useful guess' rather than prefilling with garbage."""
    from homelabsage.db import Database

    llm = _ScriptedLLM(
        [
            "(no purpose stated yet — fill in)",
            "(no purpose stated yet — fill in)",
        ]
    )
    db = Database(tmp_path / "state.sqlite")
    c = Curator(
        cfg=CuratorConfig(),
        llm=llm,
        docker_cfg=DockerSourceConfig(enabled=True),
        notes_dir=str(tmp_path),
        db=db,
    )
    asyncio.run(c.curate_one(_snapshot()))
    pending = db.list_interview_questions()
    assert pending[0].suggested_text is None


def test_suggestion_first_line_only_kept(tmp_path: Path):
    """Some models add explanatory paragraphs after the lead sentence — we
    only show the first line so the textarea stays usable."""
    from homelabsage.db import Database

    llm = _ScriptedLLM(
        [
            "(no purpose stated yet — fill in)",
            "Likely a search indexer.\n\nIt uses Elasticsearch under the hood.",
        ]
    )
    db = Database(tmp_path / "state.sqlite")
    c = Curator(
        cfg=CuratorConfig(),
        llm=llm,
        docker_cfg=DockerSourceConfig(enabled=True),
        notes_dir=str(tmp_path),
        db=db,
    )
    asyncio.run(c.curate_one(_snapshot()))
    pending = db.list_interview_questions()
    assert pending[0].suggested_text == "Likely a search indexer."


def test_generate_suggestion_returns_none_on_llm_exception(tmp_path: Path):
    class _BoomLLM:
        def is_enabled(self) -> bool:
            return True

        async def generate_text(self, prompt: str) -> str:
            raise RuntimeError("boom")

    c = Curator(
        cfg=CuratorConfig(),
        llm=_BoomLLM(),
        docker_cfg=DockerSourceConfig(enabled=True),
        notes_dir=str(tmp_path),
    )
    result = asyncio.run(c.generate_suggestion(_snapshot()))
    assert result is None


# ─── Enrichment integration ─────────────────────────────────────────────


def test_build_prompt_renders_enrichment_sections(tmp_path: Path):
    from homelabsage.enrichment import Enrichment

    cfg = CuratorConfig()
    c = Curator(
        cfg=cfg,
        llm=_FakeLLM(),
        docker_cfg=DockerSourceConfig(enabled=True),
        notes_dir=str(tmp_path),
    )
    enr = Enrichment(
        readme="# Wallos\n\nWallos tracks subscriptions.",
        docker_hub="A SaaS subscription tracker.",
        logs="2026-05-14 listening on 0.0.0.0:8282",
    )
    prompt = c.build_prompt(_snapshot(), "(none)", "(none)", enrichment=enr)

    assert "Wallos tracks subscriptions." in prompt
    assert "A SaaS subscription tracker." in prompt
    assert "listening on 0.0.0.0:8282" in prompt
    # Section headers must be present so the LLM knows what's what
    assert "Upstream README excerpt" in prompt
    assert "Docker Hub description" in prompt
    assert "Recent container logs" in prompt


def test_build_prompt_renders_none_when_enrichment_missing(tmp_path: Path):
    cfg = CuratorConfig()
    c = Curator(
        cfg=cfg,
        llm=_FakeLLM(),
        docker_cfg=DockerSourceConfig(enabled=True),
        notes_dir=str(tmp_path),
    )
    prompt = c.build_prompt(_snapshot(), "(none)", "(none)")
    # No enrichment passed → sections render as "(none)" so the LLM doesn't
    # see headers with empty bodies.
    assert "Upstream README excerpt" in prompt
    assert prompt.count("(none)") >= 3


def test_gather_enrichment_respects_flags(tmp_path: Path):
    cfg = CuratorConfig(
        fetch_readme=False, fetch_docker_hub=False, include_logs=False
    )
    c = Curator(
        cfg=cfg,
        llm=_FakeLLM(),
        docker_cfg=DockerSourceConfig(enabled=True),
        notes_dir=str(tmp_path),
    )
    enr = asyncio.run(c.gather_enrichment(_snapshot()))
    # Everything disabled — no fetches, no log probe.
    assert enr.readme is None
    assert enr.docker_hub is None
    assert enr.logs is None


def test_curate_one_passes_enrichment_to_main_prompt(tmp_path: Path, monkeypatch):
    """End-to-end: curate_one gathers enrichment and the values end up in
    the prompt the LLM actually sees."""
    from homelabsage import curator as curator_module

    async def _fake_readme(repo, *, max_chars=8000, client=None):
        return "REPO_README_MARKER"

    async def _fake_hub(image, *, max_chars=6000, client=None):
        return "DOCKER_HUB_MARKER"

    monkeypatch.setattr(curator_module.core, "fetch_github_readme", _fake_readme)
    monkeypatch.setattr(curator_module.core, "fetch_docker_hub_description", _fake_hub)
    monkeypatch.setattr(
        curator_module.core, "fetch_container_logs", lambda *a, **kw: "LOG_MARKER"
    )

    llm = _FakeLLM("## Purpose\n\nA real container note.")
    c = Curator(
        cfg=CuratorConfig(include_logs=True),
        llm=llm,
        docker_cfg=DockerSourceConfig(enabled=True),
        notes_dir=str(tmp_path),
    )
    # Bypass the docker SDK probe for logs — gather_enrichment swallows the
    # exception and the monkeypatched fetch_container_logs returns the marker.
    asyncio.run(c.curate_one(_snapshot()))

    # The (only) prompt the LLM saw must carry all three enrichment markers.
    assert llm.calls, "curate_one should have called the LLM"
    seen = llm.calls[-1]
    assert "REPO_README_MARKER" in seen
    assert "DOCKER_HUB_MARKER" in seen
    assert "LOG_MARKER" in seen


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
