"""FastAPI interface. Generation belongs to the separately supervised worker."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from agentwriter.api.middleware import BoundaryMiddleware
from agentwriter.api.security import authenticated, issue_session
from agentwriter.domain import TERMINAL_STATES, CreateRunRequest
from agentwriter.services.citations import CitationError, render_citations, validate_citations
from agentwriter.settings import Settings
from agentwriter.storage.pool import database_pool
from agentwriter.storage.repository import Repository, StorageError

logger = logging.getLogger("agentwriter.api")
WEB_ROOT = Path(__file__).parent / "web"


@dataclass(frozen=True, slots=True)
class EventContext:
    owner: str
    cursor: int
    rows: list[dict]


async def event_context(
    run_id: str,
    request: Request,
    owner: str = Depends(authenticated),
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> EventContext:
    """Validate cursor/ownership before an SSE response starts.

    Raising inside the response generator would attempt to serialize an HTTP
    error after the event stream headers were sent.
    """
    if last_event_id is not None:
        if not last_event_id.isascii() or not last_event_id.isdigit() or len(last_event_id) > 18:
            raise StorageError("invalid_cursor", "Invalid event cursor.", 422)
        after = int(last_event_id)
    if after > 2**63 - 1:
        raise StorageError("invalid_cursor", "Invalid event cursor.", 422)
    observers = request.app.state.observers
    if observers.get(owner, 0) >= 4:
        raise StorageError(
            "observer_limit", "Close another progress view before opening this one.", 429
        )
    rows = await request.app.state.repository.list_events(owner, run_id, after=after)
    return EventContext(owner, after, rows)


class EditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    parent_revision: UUID
    markdown: str = Field(min_length=1, max_length=200000)


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if configured.auth_mode != "local":
            raise RuntimeError("Hosted identity is not implemented; do not expose this development release.")
        if configured.database_url is None:
            raise RuntimeError("Set DATABASE_URL and run migrations before starting the API.")
        dsn = configured.database_url.get_secret_value()
        async with database_pool(dsn, max_size=configured.database_pool_max_size,
                                 timeout=configured.database_pool_timeout) as pool:
            app.state.repository = Repository(
                pool, dsn, max_user_runs=configured.max_user_nonterminal_runs,
                max_global_runs=configured.max_global_nonterminal_runs,
                max_run_seconds=configured.max_run_seconds,
            )
            yield

    app = FastAPI(title="AgentWriter", version="0.1.0", lifespan=lifespan)
    app.state.settings = configured
    app.state.local_session_key = secrets.token_bytes(32)
    app.state.observers = {}
    app.add_middleware(BoundaryMiddleware, max_bytes=configured.max_request_bytes)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(configured.allowed_hosts))
    if configured.allowed_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(configured.allowed_origins),
                           allow_credentials=True, allow_methods=["GET", "POST", "DELETE"],
                           allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key", "Last-Event-ID"])

    def error(request, code, message, status, retryable=False, details=None):
        payload = {"code": code, "message": message, "retryable": retryable,
                   "correlation_id": getattr(request.state, "correlation_id", "unavailable")}
        if details:
            payload["details"] = details
        return JSONResponse(payload, status_code=status)

    @app.exception_handler(StorageError)
    async def storage_error(request, exc):
        return error(request, exc.code, exc.message, exc.status_code, exc.status_code in {429, 503})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        details = [{"location": list(item["loc"]), "type": item["type"],
                    "message": item["msg"]} for item in exc.errors()]
        return error(request, "invalid_request", "Check the highlighted request fields.", 422, details=details)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        # Log class/ID only. Database DSNs or provider bodies may occur in exception strings.
        logger.error("request_failed correlation_id=%s exception_type=%s",
                     getattr(request.state, "correlation_id", "unavailable"), type(exc).__name__)
        return error(request, "service_unavailable", "The service could not complete this request.", 503, True)

    @app.get("/live")
    async def live():
        return {"status": "alive"}

    @app.get("/ready")
    async def ready(request: Request):
        ok = await request.app.state.repository.ready(freshness_seconds=configured.worker_stale_seconds)
        return JSONResponse({"status": "ready" if ok else "unavailable"}, status_code=200 if ok else 503)

    @app.get("/api/v1/session")
    async def session(request: Request, response: Response):
        token, csrf = issue_session(request)
        response.set_cookie("aw_session", token, httponly=True, samesite="strict", max_age=8 * 3600,
                            secure=request.url.scheme == "https")
        response.headers["Cache-Control"] = "no-store"
        return {"csrf_token": csrf, "mode": "local", "fixture": configured.model_provider == "fixture"}

    @app.post("/api/v1/runs", status_code=202)
    async def submit(brief: CreateRunRequest, request: Request, owner: str = Depends(authenticated),
                     idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200)):
        run = await request.app.state.repository.create_run(owner, brief.model_dump(mode="json"), idempotency_key)
        base = f"/api/v1/runs/{run['id']}"
        return {**run, "status_url": base, "events_url": base + "/events"}

    @app.get("/api/v1/runs")
    async def history(request: Request, owner: str = Depends(authenticated),
                      limit: int = Query(default=20, ge=1, le=100), before: str | None = None):
        rows = await request.app.state.repository.list_runs(owner, limit=limit, before=before)
        return {"runs": rows, "next_cursor": rows[-1]["id"] if len(rows) == limit else None}

    @app.get("/api/v1/runs/{run_id}")
    async def status(run_id: str, request: Request, owner: str = Depends(authenticated)):
        return await request.app.state.repository.get_run(owner, run_id)

    @app.post("/api/v1/runs/{run_id}/cancel")
    async def cancel(run_id: str, request: Request, owner: str = Depends(authenticated)):
        return await request.app.state.repository.cancel_run(owner, run_id)

    @app.delete("/api/v1/runs/{run_id}", status_code=204)
    async def delete(run_id: str, request: Request, owner: str = Depends(authenticated)):
        await request.app.state.repository.delete_run(owner, run_id)
        return Response(status_code=204)

    @app.get("/api/v1/runs/{run_id}/sources")
    async def sources(run_id: str, request: Request, owner: str = Depends(authenticated),
                      limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0)):
        return {"sources": await request.app.state.repository.sources(owner, run_id, limit=limit, offset=offset)}

    @app.get("/api/v1/runs/{run_id}/article")
    async def article(run_id: str, request: Request, owner: str = Depends(authenticated)):
        return await request.app.state.repository.get_article(owner, run_id)

    @app.get("/api/v1/runs/{run_id}/revisions")
    async def revisions(run_id: str, request: Request, owner: str = Depends(authenticated)):
        return {"revisions": await request.app.state.repository.list_revisions(owner, run_id)}

    @app.post("/api/v1/runs/{run_id}/revisions", status_code=201)
    async def edit(run_id: str, change: EditRequest, request: Request, owner: str = Depends(authenticated)):
        ledger = await request.app.state.repository.sources(owner, run_id)
        try:
            validate_citations(change.markdown, {source["id"]: source for source in ledger})
        except (CitationError, ValueError):
            raise StorageError("invalid_article", "Use valid source markers and omit raw HTML/external links.", 422) from None
        sources_by_id = {source["id"]: source for source in ledger}
        rendered = render_citations(change.markdown, sources_by_id)
        return await request.app.state.repository.edit_article(
            owner, run_id, str(change.parent_revision), rendered,
            citation_map={"sources": sources_by_id, "editable_markdown": change.markdown},
        )

    @app.get("/api/v1/runs/{run_id}/revisions/{revision}/download")
    async def download(run_id: str, revision: str, request: Request, owner: str = Depends(authenticated)):
        result = await request.app.state.repository.get_article(owner, run_id, revision)
        return PlainTextResponse(result["markdown"], media_type="text/markdown; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="agentwriter-{result["id"]}.md"',
            "ETag": f'"{result["content_hash"]}"', "Cache-Control": "private, no-store",
        })

    @app.get("/api/v1/runs/{run_id}/events", response_class=EventSourceResponse)
    async def events(
        run_id: str,
        request: Request,
        context: Annotated[EventContext, Depends(event_context)],
    ) -> AsyncIterator[ServerSentEvent]:
        owner, cursor, rows = context.owner, context.cursor, context.rows
        repo = request.app.state.repository
        app.state.observers[owner] = app.state.observers.get(owner, 0) + 1
        try:
            while True:
                authenticated(request)
                for row in rows:
                    cursor = row["sequence"]
                    yield ServerSentEvent(id=str(cursor), event=row["event_type"], data={
                        "schema_version": row["schema_version"], "run_id": run_id,
                        "sequence": cursor, "payload": row["payload"], "created_at": row["created_at"],
                    })
                run = await repo.get_run(owner, run_id)
                if run["state"] in TERMINAL_STATES and cursor >= run["next_event_sequence"]:
                    return
                if await request.is_disconnected():
                    return
                await asyncio.sleep(0.5)
                rows = await repo.list_events(owner, run_id, after=cursor)
        except StorageError as exc:
            yield ServerSentEvent(event="stream_error", data={"code": exc.code, "resync": True})
        finally:
            app.state.observers[owner] = max(0, app.state.observers.get(owner, 1) - 1)

    if WEB_ROOT.exists():
        app.mount("/assets", StaticFiles(directory=WEB_ROOT), name="assets")

        @app.get("/", include_in_schema=False)
        async def home():
            return FileResponse(WEB_ROOT / "index.html")

    return app
