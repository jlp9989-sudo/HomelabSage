"""LLM parser must be tolerant of common model misbehaviors:
markdown fences, prose around JSON, extra fields, wrong severity case,
and inline reasoning blocks (`<think>...</think>`) that some Qwen / Deepseek
backends emit in the `content` field instead of `reasoning_content`."""

import pytest

from homelabsage.llm import (
    _parse_analysis,
    _resolve_chat_completions_url,
    _strip_think_blocks,
    build_prompt,
)
from homelabsage.models import Severity, Update

JSON_GOOD = """{
  "severity": "high",
  "summary": "Major version with security fixes.",
  "breaking_changes": ["API v1 removed"],
  "config_obsolete": [],
  "new_features_relevant": ["faster vector search"],
  "action_required": true,
  "recommended_action": "Backup before upgrading"
}"""


def test_parser_handles_clean_json():
    a = _parse_analysis(JSON_GOOD)
    assert a is not None
    assert a.severity is Severity.HIGH
    assert a.action_required is True
    assert "API v1 removed" in a.breaking_changes


def test_parser_strips_markdown_fence():
    raw = f"```json\n{JSON_GOOD}\n```"
    a = _parse_analysis(raw)
    assert a is not None and a.severity is Severity.HIGH


def test_parser_strips_surrounding_prose():
    raw = f"Sure, here is the analysis:\n\n{JSON_GOOD}\n\nLet me know if you need more."
    a = _parse_analysis(raw)
    assert a is not None and a.action_required is True


def test_parser_normalizes_severity_case():
    a = _parse_analysis('{"severity":"CRITICAL","summary":"x"}')
    assert a is not None and a.severity is Severity.CRITICAL


def test_parser_returns_none_on_garbage():
    assert _parse_analysis("not even close to JSON") is None


def test_parser_fallback_keeps_summary_when_schema_wrong():
    # Missing 'severity' but has 'summary' — best effort
    a = _parse_analysis('{"summary":"useful sentence"}')
    assert a is not None
    assert a.severity is Severity.INFO
    assert "useful sentence" in a.summary


def test_build_prompt_includes_all_context_fields():
    u = Update(
        source="docker",
        subject="mealie",
        current_version="3.16.0",
        new_version="3.17.0",
        release_url="https://github.com/x/y",
        release_notes="changelog body",
        context={"image": "ghcr.io/x/y:3.17"},
    )
    p = build_prompt(u, notes="user note about mealie")
    assert "mealie" in p
    assert "3.16.0" in p and "3.17.0" in p
    assert "changelog body" in p
    assert "user note about mealie" in p
    assert "image" in p  # context dict serialized


def test_build_prompt_carries_env_var_rename_rule():
    """Rule must be present so the LLM is instructed to call out renamed env vars."""
    u = Update(source="docker", subject="x", current_version="1", new_version="2")
    p = build_prompt(u)
    assert "env" in p.lower() and "renamed" in p.lower()
    # And the suggested phrasing must be there so the model knows the output shape.
    assert "OLD_NAME" in p and "NEW_NAME" in p


def test_build_prompt_carries_db_migration_rule():
    u = Update(source="docker", subject="x", current_version="1", new_version="2")
    p = build_prompt(u)
    assert "migration" in p.lower()
    # The verbatim warning string must be in the prompt so the model can mirror it.
    assert "do not interrupt the first start after upgrade" in p


def test_build_prompt_truncates_huge_release_notes():
    u = Update(
        source="docker", subject="x", current_version="1", new_version="2",
        release_notes="A" * 50000,
    )
    p = build_prompt(u)
    # Release notes are capped at 15k chars in the template. We allow a small
    # margin for incidental "A" characters elsewhere in the template literal.
    assert 15000 <= p.count("A") <= 15020
    # The bound is total = 15k (capped release notes) + ≤5k (template overhead +
    # accumulated prompt rules). The point of the check is that release-note
    # truncation IS happening — without it the prompt would be ≥50k chars.
    # Bumping the overhead allowance is fine; bumping past the cap (i.e. >20k
    # total when release_notes were 50k) is not.
    assert len(p) < 20000


# ─── <think> stripper ────────────────────────────────────────────────────

def test_strip_think_passthrough_when_no_tag():
    txt = "Just a regular response with no special tags."
    assert _strip_think_blocks(txt) == txt


def test_strip_think_passthrough_empty():
    assert _strip_think_blocks("") == ""


def test_strip_think_removes_single_block():
    raw = (
        "<think>\nOkay let's tackle this. Container is mealie, "
        "image ghcr.io/...\n</think>\n\n"
        "Self-hosted recipe manager for the household."
    )
    out = _strip_think_blocks(raw)
    assert "<think" not in out.lower()
    assert "Okay let's tackle" not in out
    assert out == "Self-hosted recipe manager for the household."


def test_strip_think_removes_multiple_blocks():
    raw = (
        "<think>first reasoning</think>"
        "Purpose line.\n"
        "<think>second pass</think>\n"
        "- bullet one"
    )
    out = _strip_think_blocks(raw)
    assert "<think" not in out.lower()
    assert "first reasoning" not in out and "second pass" not in out
    assert "Purpose line." in out
    assert "- bullet one" in out


def test_strip_think_tolerates_attributes():
    # Some backends emit <think type="reasoning"> or similar.
    raw = '<think type="reasoning">noise</think>\nReal content'
    assert _strip_think_blocks(raw) == "Real content"


def test_strip_think_is_case_insensitive():
    raw = "<Think>Pondering</Think>\nAnswer"
    assert _strip_think_blocks(raw) == "Answer"


def test_strip_think_handles_multiline_block():
    raw = """<think>
Let me consider:
- option A
- option B
- option C
The user wants…
</think>

Final answer here."""
    out = _strip_think_blocks(raw)
    assert "<think" not in out.lower()
    assert "option A" not in out
    assert out == "Final answer here."


def test_strip_think_preserves_json_after_block():
    """Critical for the analyzer code path: the JSON must survive intact."""
    raw = '<think>weighing severity</think>\n{"severity":"high","summary":"x"}'
    out = _strip_think_blocks(raw)
    assert out == '{"severity":"high","summary":"x"}'
    # And the parser then accepts it
    a = _parse_analysis(out)
    assert a is not None and a.severity is Severity.HIGH


def test_strip_think_unclosed_prefix_with_json_after():
    """Truncated stream: <think> opens, never closes, then real JSON follows."""
    raw = '<think>started reasoning but stream cut\n{"severity":"high","summary":"x"}'
    out = _strip_think_blocks(raw)
    # The prefix regex should eat up to the first '{' so the JSON parser still works.
    assert out.startswith("{")
    a = _parse_analysis(out)
    assert a is not None and a.severity is Severity.HIGH


@pytest.mark.parametrize(
    "endpoint, expected",
    [
        # OpenAI default — bare base URL, append /v1/chat/completions
        ("https://api.openai.com", "https://api.openai.com/v1/chat/completions"),
        # Trailing slash must be tolerated
        ("https://api.openai.com/", "https://api.openai.com/v1/chat/completions"),
        # Groq — base URL + /openai segment, no version: append /v1/chat/completions
        ("https://api.groq.com/openai", "https://api.groq.com/openai/v1/chat/completions"),
        # OpenRouter — base URL + /api segment, no version: append /v1/chat/completions
        ("https://openrouter.ai/api", "https://openrouter.ai/api/v1/chat/completions"),
        # Gemini — already includes /v1beta version segment: append only /chat/completions
        (
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        ),
        # User pasted the full v1 path (common copy-paste from OpenAI docs):
        # detect the version segment, append /chat/completions only — no double /v1
        ("https://api.openai.com/v1", "https://api.openai.com/v1/chat/completions"),
        # User pasted the full URL: leave it alone
        (
            "https://api.openai.com/v1/chat/completions",
            "https://api.openai.com/v1/chat/completions",
        ),
    ],
)
def test_resolve_chat_completions_url(endpoint, expected):
    assert _resolve_chat_completions_url(endpoint) == expected
