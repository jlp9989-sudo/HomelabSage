"""External webhook receivers — `/api/inbox/*` + GitHub release.

Split out of `routes_updates.py` in v0.11.3. These endpoints are
auth-bypassed at the app level so external services can reach them
without HTTP Basic Auth — therefore each endpoint carries its own
authn/authz logic:

  POST /api/inbox/{source}              free-form push: validates
                                        the source slug and payload
                                        shape, no shared secret.
  POST /api/webhook/github-release      GitHub Release webhook with
                                        mandatory HMAC-SHA256 via
                                        `GITHUB_RELEASE_WEBHOOK_SECRET`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request

from ..db import Database
from ..models import AnalyzedUpdate, Update


def register_inbox_routes(app: FastAPI, db: Database) -> None:
    @app.post("/api/inbox/{source}")
    async def api_inbox(source: str, request: Request) -> dict:
        """Webhook receiver — external systems POST update events here.

        Accepts a flexible payload. Required: `subject`, `new_version`.
        Optional: `current_version` (defaults to "(unknown)"),
        `release_url` (must be http(s):// and ≤2048 chars),
        `release_notes` (≤64 KB), `context` (dict).

        Source string becomes `Update.source`. Restricted to
        `[a-z0-9_-]{1,32}` so a malicious caller can't pollute the
        dashboard with display-control characters.

        Body size: capped at 1 MB. `release_notes` capped at 64 KB
        before persistence so a single big POST can't bloat the DB.
        """
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", source):
            raise HTTPException(
                400, "source must match [a-z0-9_-]{1,32}",
            )
        # Hard request-body cap. Reject before parsing so a 100 MB POST
        # can't fill memory just to be rejected as "payload too large".
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > 1024 * 1024:
                    raise HTTPException(413, "payload exceeds 1 MB cap")
            except ValueError:
                pass
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(400, "payload must be a JSON object")
        subject = payload.get("subject")
        new_version = payload.get("new_version")
        if not isinstance(subject, str) or not subject.strip():
            raise HTTPException(400, "subject is required")
        if not isinstance(new_version, str) or not new_version.strip():
            raise HTTPException(400, "new_version is required")
        # subject + version: cap to prevent unbounded growth + control
        # chars that would break UI/Notion rendering.
        if len(subject) > 200:
            raise HTTPException(400, "subject too long (max 200)")
        if len(new_version) > 100:
            raise HTTPException(400, "new_version too long (max 100)")
        current_version = payload.get("current_version") or "(unknown)"
        if not isinstance(current_version, str):
            raise HTTPException(400, "current_version must be a string")
        if len(current_version) > 100:
            raise HTTPException(400, "current_version too long (max 100)")
        # release_url: only http(s); reject javascript: / data: shells.
        release_url = payload.get("release_url") or None
        if release_url is not None:
            if not isinstance(release_url, str):
                raise HTTPException(400, "release_url must be a string")
            if len(release_url) > 2048:
                raise HTTPException(400, "release_url too long (max 2048)")
            scheme = urlparse(release_url).scheme.lower()
            if scheme not in ("http", "https"):
                raise HTTPException(
                    400, "release_url scheme must be http or https",
                )
        release_notes = payload.get("release_notes")
        if release_notes is not None and not isinstance(release_notes, str):
            raise HTTPException(400, "release_notes must be a string")
        if isinstance(release_notes, str):
            release_notes = release_notes[:65536]  # 64 KB hard cap
        ctx = payload.get("context") or {}
        if not isinstance(ctx, dict):
            raise HTTPException(400, "context must be an object")
        upd = Update(
            source=source,
            subject=subject.strip(),
            current_version=current_version,
            new_version=new_version.strip(),
            release_url=release_url,
            release_notes=release_notes,
            context=ctx,
        )
        analyzed = AnalyzedUpdate(update=upd)
        db.upsert(analyzed)
        return {"ok": True, "id": analyzed.id, "source": source}

    @app.post("/api/webhook/github-release")
    async def api_inbox_github_release(request: Request) -> dict:
        """GitHub Release webhook receiver.

        Configure in your repo: Settings → Webhooks → Add webhook,
        URL = `https://<your-host>/api/webhook/github-release`, content
        type `application/json`, events `Releases`.

        Optional shared-secret verification: set
        `GITHUB_RELEASE_WEBHOOK_SECRET` in env and configure the same
        secret in the GitHub UI. When set, requests without a valid
        `X-Hub-Signature-256` are rejected.

        Only `action: released` (not draft / pre-release / edit) is
        accepted to avoid duplicates from staging releases.
        """
        # Hard size cap — GitHub bodies are typically <100 KB but
        # release notes can balloon.
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > 1024 * 1024:
                    raise HTTPException(413, "payload exceeds 1 MB cap")
            except ValueError:
                pass

        raw_body = await request.body()
        secret = os.environ.get("GITHUB_RELEASE_WEBHOOK_SECRET", "")
        # The endpoint is auth-bypassed at the app level so GitHub
        # can reach it without Basic Auth — therefore the HMAC IS
        # the auth. Refuse the request when no secret is configured
        # rather than silently accepting unsigned requests.
        if not secret:
            raise HTTPException(
                503,
                "GITHUB_RELEASE_WEBHOOK_SECRET not configured — "
                "set it in the environment to enable this endpoint",
            )
        sig_header = request.headers.get("X-Hub-Signature-256", "")
        if not sig_header.startswith("sha256="):
            raise HTTPException(401, "missing or malformed signature")
        expected = "sha256=" + hmac.new(
            secret.encode(), raw_body, hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, sig_header):
            raise HTTPException(401, "signature mismatch")

        try:
            payload = json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            raise HTTPException(400, "invalid JSON")  # noqa: B904
        if not isinstance(payload, dict):
            raise HTTPException(400, "payload must be a JSON object")

        action = payload.get("action") or ""
        if action != "released":
            return {"ok": True, "skipped": True,
                    "reason": f"action={action} (only 'released' accepted)"}

        release = payload.get("release") or {}
        repo = payload.get("repository") or {}
        if not isinstance(release, dict) or not isinstance(repo, dict):
            raise HTTPException(400, "missing release / repository envelope")

        tag = release.get("tag_name") or ""
        repo_full = repo.get("full_name") or ""
        if not tag or not repo_full:
            raise HTTPException(400, "release.tag_name + repository.full_name required")
        if len(tag) > 100 or len(repo_full) > 200:
            raise HTTPException(400, "tag or repo name too long")

        release_url = release.get("html_url") or ""
        if release_url:
            scheme = urlparse(release_url).scheme.lower()
            if scheme not in ("http", "https"):
                release_url = ""
            if len(release_url) > 2048:
                release_url = ""
        body_raw = release.get("body") or ""
        body_md = body_raw[:65536] if isinstance(body_raw, str) else ""

        upd = Update(
            source="github-release",
            subject=repo_full,
            current_version="(unknown)",
            new_version=tag,
            release_url=release_url or None,
            release_notes=body_md,
            context={"webhook": True, "event_action": action},
        )
        analyzed = AnalyzedUpdate(update=upd)
        db.upsert(analyzed)
        return {"ok": True, "id": analyzed.id,
                "subject": repo_full, "tag": tag}
