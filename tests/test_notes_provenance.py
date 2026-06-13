"""Curator note provenance: the analyzer must not treat HomelabSage's own
auto-generated notes as operator constraints.

A note file carrying the curator footer (`<!-- curator: name@digest -->`) was
written by the tool itself. When such a section reaches the analyzer it is
tagged in its header and has the footer stripped, so the prompt's
versionlock / dependency rules (which only apply to operator notes) don't
inherit max confidence from the system's own earlier guess.
"""

from __future__ import annotations

from homelabsage.curator.helpers import FOOTER_RE as _CANONICAL_FOOTER_RE
from homelabsage.notes import _CURATOR_FOOTER_RE, _GENERATED_TAG, NotesProvider

_FOOTER = "<!-- curator: mealie@abc123 -->"


def test_footer_regex_does_not_drift_from_curator():
    """The local detector must agree with the curator's canonical footer
    regex on a real footer string, so the two can't silently diverge."""
    assert _CURATOR_FOOTER_RE.search(_FOOTER) is not None
    assert _CANONICAL_FOOTER_RE.search(_FOOTER) is not None
    # Plain operator text must match neither.
    assert _CURATOR_FOOTER_RE.search("## Mealie\nVersionlocked on 3.16.") is None


def test_auto_generated_note_is_tagged_and_footer_stripped(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "mealie.md").write_text(
        "## Mealie\n"
        "Self-hosted recipe manager. Versionlocked feel — pin before upgrade.\n\n"
        f"{_FOOTER}\n"
    )
    ctx = NotesProvider(notes_dir=notes, max_chars=2000).context_for("mealie")
    assert _GENERATED_TAG in ctx                 # header flags provenance
    assert "Self-hosted recipe manager" in ctx   # body still useful as background
    assert "curator:" not in ctx                 # raw footer comment stripped


def test_operator_note_is_not_tagged(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "infra.md").write_text(
        "## Mealie\nVersionlocked on 3.16 due to plugin compat.\n"
    )
    ctx = NotesProvider(notes_dir=notes, max_chars=2000).context_for("mealie")
    assert _GENERATED_TAG not in ctx
    assert "Versionlocked on 3.16" in ctx


def test_mixed_dir_tags_only_the_generated_file(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "operator.md").write_text(
        "## Mealie\nDo NOT upgrade past 3.16 — breaks my plugin.\n"
    )
    (notes / "auto.md").write_text(
        "## Mealie\nRecipe manager, runs fine.\n\n" + _FOOTER + "\n"
    )
    ctx = NotesProvider(notes_dir=notes, max_chars=4000).context_for("mealie")
    # The operator's hard constraint survives untagged; the auto note is tagged.
    assert "Do NOT upgrade past 3.16" in ctx
    assert _GENERATED_TAG in ctx
    # Exactly one section carries the tag (the auto one), not the operator's.
    assert ctx.count(_GENERATED_TAG) == 1
