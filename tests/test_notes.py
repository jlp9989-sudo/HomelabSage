import pytest

from homelabsage.notes import NotesEditor, NotesProvider, _match_score, _sections


def test_sections_splits_by_headers():
    md = "intro\n\n## Foo\nfoo body\n\n## Bar\nbar body\n### Bar sub\nsubtext\n"
    secs = _sections(md)
    headers = [h for h, _ in secs]
    assert headers == ["", "Foo", "Bar", "Bar > Bar sub"]


def test_match_score_weights_header_higher_than_body():
    s_header = _match_score(["mealie"], "Mealie deployment", "")
    s_body_once = _match_score(["mealie"], "general", "mealie body")
    assert s_header > s_body_once
    assert s_body_once == 1
    assert s_header == 3


def test_match_score_ignores_short_keywords():
    assert _match_score(["go"], "Go programming notes", "go go go") == 0


def test_notes_provider_picks_relevant_section(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "infra.md").write_text(
        "## Mealie\n"
        "Versionlocked on 3.16 due to plugin compat.\n\n"
        "## Vaultwarden\n"
        "Critical security path.\n"
    )
    provider = NotesProvider(notes_dir=notes, max_chars=400)
    ctx = provider.context_for("mealie")
    assert "Versionlocked on 3.16" in ctx
    assert "Vaultwarden" not in ctx  # different subject, no match


def test_notes_provider_respects_max_chars(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "n.md").write_text("## mealie\n" + ("x" * 5000))
    out = NotesProvider(notes_dir=notes, max_chars=200).context_for("mealie")
    assert len(out) <= 220  # 200 chars + small header overhead


def test_notes_provider_always_includes_extra_docs(tmp_path):
    extra = tmp_path / "POLICY.md"
    extra.write_text("global policy text")
    provider = NotesProvider(notes_dir=None, extra_docs=[extra], max_chars=1000)
    ctx = provider.context_for("anything")
    assert "global policy text" in ctx


def test_notes_editor_rejects_path_traversal(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    ed = NotesEditor(notes)
    with pytest.raises((PermissionError, ValueError)):
        ed.read("../../etc/passwd")


def test_notes_editor_rejects_bad_extension(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    ed = NotesEditor(notes)
    with pytest.raises(ValueError):
        ed.write("hack.sh", "#!/bin/sh\necho pwned")


def test_notes_editor_round_trip(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    ed = NotesEditor(notes)
    ed.write("a.md", "hello")
    assert ed.read("a.md") == "hello"
    listed = ed.list()
    assert len(listed) == 1 and listed[0]["name"] == "a.md"
    ed.delete("a.md")
    assert ed.list() == []


def test_notes_editor_disabled_when_dir_missing():
    ed = NotesEditor(None)
    assert ed.enabled is False
    assert ed.list() == []
