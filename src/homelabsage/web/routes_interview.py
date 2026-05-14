"""Routes for the curator interview workflow.

When the curator gives up on a container's purpose (Rule 7 fallback), it
persists an `InterviewQuestion` instead of writing a useless note. This
module exposes them in the UI:

  GET  /interview              full list with answer/dismiss forms
  POST /interview/{id}/answer  record answer, re-curate, redirect
  POST /interview/{id}/dismiss soft-delete, redirect
  GET  /interview/banner       HTMX partial — empty if no pending,
                               otherwise a clickable banner

Re-curate is awaited inline (one LLM call, ~10-30s) — the user clicked
"Save answer" and wants to see the result. APScheduler's worker is
unrelated; this runs on the request thread.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment

from ..config import Config
from ..db import Database
from ..models import InterviewStatus

log = logging.getLogger(__name__)


def register_interview_routes(
    app: FastAPI, cfg: Config, db: Database, env: Environment
) -> None:
    @app.get("/interview", response_class=HTMLResponse)
    async def interview_list_view() -> HTMLResponse:
        pending = db.list_interview_questions(status=InterviewStatus.PENDING)
        answered = db.list_interview_questions(status=InterviewStatus.ANSWERED, limit=20)
        dismissed = db.list_interview_questions(status=InterviewStatus.DISMISSED, limit=20)
        tmpl = env.get_template("interview_list.html")
        return HTMLResponse(
            tmpl.render(pending=pending, answered=answered, dismissed=dismissed)
        )

    @app.get("/interview/banner", response_class=HTMLResponse)
    async def interview_banner() -> HTMLResponse:
        count = db.count_interview_questions(InterviewStatus.PENDING)
        if count == 0:
            # Empty body → HTMX swap leaves the slot empty.
            return HTMLResponse("")
        tmpl = env.get_template("_interview_banner.html")
        return HTMLResponse(tmpl.render(count=count))

    @app.post("/interview/{question_id}/answer")
    async def interview_answer_post(
        question_id: int, text: str = Form(...)
    ) -> RedirectResponse:
        text = text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="answer text is required")
        q = db.get_interview_question(question_id)
        if q is None:
            raise HTTPException(status_code=404, detail="question not found")
        db.answer_interview_question(question_id, text)

        # Re-curate that container with the user-provided purpose. Lazy
        # imports keep the heavy docker SDK out of the cold web path.
        try:
            from ..curator import Curator
            from ..llm import LLMClient

            llm = LLMClient(cfg.llm)
            curator = Curator(
                cfg.curator,
                llm,
                cfg.sources.docker,
                notes_dir=cfg.notes.notes_dir,
                db=db,
            )
            try:
                snapshots = curator.discover_targets(only=[q.container_name])
                if snapshots:
                    await curator.curate_one(
                        snapshots[0], force=True, user_purpose=text
                    )
                else:
                    log.warning(
                        "interview: container %r not running — answer recorded "
                        "but note not regenerated",
                        q.container_name,
                    )
            finally:
                curator.close()
        except Exception:
            # Don't let a re-curate failure swallow the answer the user just
            # saved — they can re-trigger from the dashboard via "Run now".
            log.exception(
                "interview: re-curate failed for %s after answer", q.container_name
            )

        return RedirectResponse("/interview", status_code=303)

    @app.post("/interview/{question_id}/dismiss")
    async def interview_dismiss_post(question_id: int) -> RedirectResponse:
        if db.get_interview_question(question_id) is None:
            raise HTTPException(status_code=404, detail="question not found")
        db.dismiss_interview_question(question_id)
        return RedirectResponse("/interview", status_code=303)
