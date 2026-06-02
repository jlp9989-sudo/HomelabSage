"""Where state, notes and curator output live on disk."""

from __future__ import annotations

from pydantic import BaseModel, Field


class StorageConfig(BaseModel):
    database_path: str = Field(
        "./data/state.sqlite",
        description="SQLite file for run state. Absolute path or relative to the working directory.",
        json_schema_extra={"ui_widget": "path", "ui_path_kind": "file"},
    )


class NotesConfig(BaseModel):
    """User's homelab notes — extra context for the LLM.

    `notes_dir`: directory scanned for .md/.txt files; sections are matched
    against each update's subject so the LLM gets just the relevant pieces.

    `extra_docs`: a few files always injected in full (e.g. CLAUDE.md).
    Keep these short — they cost tokens on every LLM call.
    """

    notes_dir: str = Field(
        "",
        description="Directory scanned for .md/.txt files. Leave empty to skip notes injection.",
        json_schema_extra={"ui_widget": "path", "ui_path_kind": "directory"},
    )
    extra_docs: list[str] = Field(default_factory=list)
    max_chars: int = 4000


class CuratorConfig(BaseModel):
    """Curator — generates one Markdown note per container so the rest of
    the pipeline always has fresh, written-up context to feed the LLM.

    `output_dir`: where notes are written. If empty, falls back to
    `notes.notes_dir` (the same directory the analyzer reads from).

    `prompt_template_path`: path to a custom .txt prompt template. If empty,
    the built-in default is used. The template may include the placeholders
    listed in `curator.PROMPT_PLACEHOLDERS`; unknown placeholders are kept
    verbatim so missing data never crashes the run.

    `style_examples`: paths to existing .md notes to inject as few-shot
    examples (e.g. `notes/example.md`). Optional — leave empty to skip.

    `recent_releases`: how many recent GitHub releases to forward to the LLM
    as upstream context.

    `max_release_chars`: per-prompt cap on combined release-notes text.
    """

    enabled: bool = True
    output_dir: str = Field(
        "",
        description="Where curator notes are written. Leave empty to write into `notes.notes_dir`.",
        json_schema_extra={"ui_widget": "path", "ui_path_kind": "directory"},
    )
    prompt_template_path: str = Field(
        "",
        description="Path to a custom prompt .txt. Leave empty to use the built-in default.",
        json_schema_extra={"ui_widget": "path", "ui_path_kind": "file"},
    )
    style_examples: list[str] = Field(default_factory=list)
    recent_releases: int = 5
    max_release_chars: int = 8000

    # Enrichment knobs — extra context fetched per container before the LLM
    # is called. Each fetch is best-effort; failures don't break the curate.
    fetch_readme: bool = Field(
        True,
        description="Fetch the upstream repo's README.md (raw.githubusercontent.com) and inject an excerpt into the curator prompt.",
    )
    fetch_docker_hub: bool = Field(
        True,
        description="When the image lives on Docker Hub, fetch its `full_description` as a fallback context source.",
    )
    include_logs: bool = Field(
        True,
        description="Include the container's last ~30 log lines as live context. Disable if you don't want logs forwarded to the LLM.",
    )
    cross_reference_notes: bool = Field(
        True,
        description="Grep the user's notes/memory dirs for the container name and inject matching snippets into the prompt — turns generic suggestions into ones tailored to YOUR setup.",
    )
    cross_reference_dirs: list[str] = Field(
        default_factory=lambda: ["/opt/claude-memory", "/opt/notes"],
        description="Directories searched by `cross_reference_notes`. Mount them read-only in your compose file.",
    )
    max_readme_chars: int = 8000
    max_docker_hub_chars: int = 6000
    max_logs_chars: int = 3000
    max_user_context_chars: int = 4000
    log_tail_lines: int = 30

    # Incremental hook — after the analyzer concludes an update is risky,
    # append a one-line summary to the curator's note so the file
    # accumulates trap history without a manual recurate.
    append_analysis_log: bool = Field(
        True,
        description=(
            "When the analyzer flags an update as 'hold' or with breaking "
            "changes, append a dated bullet to the curator note for the "
            "service. Only ever touches files written by the curator (with "
            "the `<!-- curator: ... -->` footer); manual notes are left alone."
        ),
    )
    analysis_log_max_lines: int = Field(
        20,
        description=(
            "Maximum entries kept in the appended `## Update log` section. "
            "Older entries drop off the tail."
        ),
    )
