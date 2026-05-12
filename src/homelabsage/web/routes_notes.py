"""Routes for the in-browser notes editor.

  GET  /notes           list of `*.md` / `*.txt` files in `notes_dir`
  GET  /notes/edit      editor view (existing or new file)
  POST /notes/save      write + redirect
  POST /notes/delete    delete + redirect

Path-traversal safety lives in `NotesEditor`, not here — the route layer
only forwards the form input.
"""

from __future__ import annotations

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment

from ..notes import NotesEditor


def register_notes_routes(app: FastAPI, editor: NotesEditor, env: Environment) -> None:
    @app.get("/notes", response_class=HTMLResponse)
    async def notes_list() -> HTMLResponse:
        tmpl = env.get_template("notes_list.html")
        return HTMLResponse(
            tmpl.render(
                files=editor.list(),
                enabled=editor.enabled,
                notes_dir=str(editor.dir) if editor.dir else "",
            )
        )

    @app.get("/notes/edit", response_class=HTMLResponse)
    async def notes_edit(filename: str | None = None) -> HTMLResponse:
        """Editor view. If `filename` is None or new, show empty editor."""
        tmpl = env.get_template("notes_edit.html")
        if not editor.enabled:
            raise HTTPException(400, "notes_dir not configured")
        is_new = not filename
        content = ""
        if filename:
            try:
                content = editor.read(filename)
            except (PermissionError, ValueError, FileNotFoundError) as e:
                raise HTTPException(404, str(e)) from e
        return HTMLResponse(
            tmpl.render(filename=filename or "", content=content, is_new=is_new)
        )

    @app.post("/notes/save")
    async def notes_save(
        filename: str = Form(...), content: str = Form(""),
    ) -> RedirectResponse:
        try:
            editor.write(filename, content)
        except (PermissionError, ValueError) as e:
            raise HTTPException(400, str(e)) from e
        return RedirectResponse("/notes", status_code=303)

    @app.post("/notes/delete")
    async def notes_delete(filename: str = Form(...)) -> RedirectResponse:
        try:
            editor.delete(filename)
        except (PermissionError, ValueError) as e:
            raise HTTPException(400, str(e)) from e
        return RedirectResponse("/notes", status_code=303)
