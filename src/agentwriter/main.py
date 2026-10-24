"""FastAPI interface. Generation belongs to the separately supervised worker."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.sse import EventSourceResponse, format_sse_event
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from agentwriter.api.middleware import BoundaryMiddleware
from agentwriter.api.security import (
    SESSION_COOKIE,
    authenticated,
    create_session,
    refresh_csrf,
    revoke_current,
)
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
    state: str
    next_sequence: int


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
    settings = request.app.state.settings
    observers = request.app.state.observers
    if observers.get(owner, 0) >= settings.max_observers_per_user:
        raise StorageError(
            "observer_limit", "Close another progress view before opening this one.", 429
        )
    run = await request.app.state.repository.get_run(owner, run_id)
    rows = await request.app.state.repository.list_events(owner, run_id, after=after)
    return EventContext(owner, after, rows, run["state"], run["next_event_sequence"])


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
                graph_version=configured.graph_version,
            )
            with contextlib.suppress(Exception):
                await app.state.repository.prune_sessions()
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

    def set_session_cookie(request: Request, response: Response, token: str) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            samesite="strict",
            max_age=configured.session_ttl_hours * 3600,
            secure=request.url.scheme == "https" or configured.app_env in {"staging", "production"},
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"

    @app.post("/api/v1/session")
    async def start_session(request: Request, response: Response):
        token, csrf = await create_session(request)
        set_session_cookie(request, response, token)
        return {
            "csrf_token": csrf,
            "mode": "local",
            "fixture": configured.model_provider == "fixture"
            or configured.research_provider == "fixture",
            "expires_in": configured.session_ttl_hours * 3600,
        }

    @app.get("/api/v1/session")
    async def session(request: Request, owner: str = Depends(authenticated)):
        csrf = await refresh_csrf(request)
        return {
            "csrf_token": csrf,
            "mode": "local",
            "owner": owner,
            "fixture": configured.model_provider == "fixture"
            or configured.research_provider == "fixture",
            "expires_in": configured.session_ttl_hours * 3600,
        }

    @app.delete("/api/v1/session", status_code=204)
    async def logout(request: Request):
        await revoke_current(request)
        response = Response(status_code=204, headers={"Cache-Control": "no-store"})
        response.delete_cookie(
            SESSION_COOKIE,
            path="/",
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https" or configured.app_env in {"staging", "production"},
        )
        return response

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
        repo = request.app.state.repository
        run = await repo.get_run(owner, run_id)
        progress = await repo.run_progress(owner, run_id)
        return {
            **run,
            "progress": progress,
            "research_limitations": run.get("research_limitations") or [],
        }

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

    @app.get("/api/v1/runs/{run_id}/events")
    async def events(
        run_id: str,
        request: Request,
        context: Annotated[EventContext, Depends(event_context)],
    ):
        owner, cursor, rows = context.owner, context.cursor, context.rows
        repo = request.app.state.repository
        if context.state in TERMINAL_STATES and not rows and cursor >= context.next_sequence:
            # Terminal replay with nothing new is an ordinary empty response,
            # not an event stream that immediately closes.
            return Response(status_code=204, headers={"Cache-Control": "no-store"})

        async def stream() -> AsyncIterator[bytes]:
            nonlocal cursor, rows
            app.state.observers[owner] = app.state.observers.get(owner, 0) + 1
            last_emit = time.monotonic()
            try:
                while True:
                    try:
                        await authenticated(request)
                    except StorageError as exc:
                        yield format_sse_event(
                            event="stream_error",
                            data_str=json.dumps({"code": exc.code, "resync": True}),
                        )
                        return
                    for row in rows:
                        cursor = row["sequence"]
                        last_emit = time.monotonic()
                        payload = {
                            "schema_version": row["schema_version"],
                            "run_id": run_id,
                            "sequence": cursor,
                            "payload": row["payload"],
                            "created_at": row["created_at"],
                        }
                        yield format_sse_event(
                            id=str(cursor),
                            event=row["event_type"],
                            data_str=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        )
                    run = await repo.get_run(owner, run_id)
                    if run["state"] in TERMINAL_STATES and cursor >= run["next_event_sequence"]:
                        return
                    if await request.is_disconnected():
                        return
                    if time.monotonic() - last_emit >= configured.sse_heartbeat_seconds:
                        yield format_sse_event(comment="heartbeat")
                        last_emit = time.monotonic()
                    await asyncio.sleep(configured.sse_poll_seconds)
                    rows = await repo.list_events(owner, run_id, after=cursor)
            except StorageError as exc:
                yield format_sse_event(
                    event="stream_error",
                    data_str=json.dumps({"code": exc.code, "resync": True}),
                )
            finally:
                app.state.observers[owner] = max(0, app.state.observers.get(owner, 1) - 1)

        return EventSourceResponse(
            stream(),
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/metrics")
    async def metrics(request: Request):
        if configured.metrics_token is not None:
            supplied = request.headers.get("authorization", "")
            expected = f"Bearer {configured.metrics_token.get_secret_value()}"
            if not hmac.compare_digest(supplied, expected):
                raise StorageError("metrics_unauthorized", "Metrics require a bearer token.", 401)
        else:
            try:
                loopback = ipaddress.ip_address(request.client.host if request.client else "").is_loopback
            except ValueError:
                loopback = False
            if not loopback:
                raise StorageError("metrics_unauthorized", "Metrics are limited to this computer.", 403)
        snapshot = await request.app.state.repository.metrics(
            stale_seconds=configured.worker_stale_seconds
        )
        lines = [
            "# HELP agentwriter_runs Number of retained runs by durable state.",
            "# TYPE agentwriter_runs gauge",
        ]
        for state, count in sorted(snapshot["states"].items()):
            lines.append(f'agentwriter_runs{{state="{state}"}} {int(count)}')
        queue = snapshot["queue"]
        lines.extend(
            [
                "# HELP agentwriter_queue_runs Runs waiting or executing.",
                "# TYPE agentwriter_queue_runs gauge",
                f'agentwriter_queue_runs{{phase="waiting"}} {int(queue["waiting"])}',
                f'agentwriter_queue_runs{{phase="running"}} {int(queue["running"])}',
                f'agentwriter_queue_runs{{phase="cancelling"}} {int(queue["cancelling"])}',
                "# HELP agentwriter_queue_oldest_wait_seconds Age of the oldest waiting run.",
                "# TYPE agentwriter_queue_oldest_wait_seconds gauge",
                f"agentwriter_queue_oldest_wait_seconds {float(queue['oldest_wait_seconds'])}",
                "# HELP agentwriter_runs_overdue Nonterminal runs past their deadline.",
                "# TYPE agentwriter_runs_overdue gauge",
                f"agentwriter_runs_overdue {int(queue['overdue'])}",
                "# HELP agentwriter_workers Worker registry by health.",
                "# TYPE agentwriter_workers gauge",
                f'agentwriter_workers{{state="alive"}} {int(snapshot["workers"]["alive"])}',
                f'agentwriter_workers{{state="draining"}} {int(snapshot["workers"]["draining"])}',
                f'agentwriter_workers{{state="dead"}} {int(snapshot["workers"]["dead"])}',
                "# HELP agentwriter_worker_active_runs Claims currently owned by workers.",
                "# TYPE agentwriter_worker_active_runs gauge",
                f"agentwriter_worker_active_runs {int(snapshot['workers']['active_runs'])}",
                "# HELP agentwriter_provider_calls Provider calls in the last 24 hours.",
                "# TYPE agentwriter_provider_calls gauge",
            ]
        )
        for status_name, count in sorted(snapshot["provider_calls_24h"].items()):
            safe = "".join(ch for ch in str(status_name) if ch.isalnum() or ch in "_-")
            lines.append(f'agentwriter_provider_calls{{status="{safe or "unknown"}"}} {int(count)}')
        spend = snapshot["spend_today"]
        lines.extend(
            [
                "# HELP agentwriter_spend_today_usd Reserved or known provider spend today.",
                "# TYPE agentwriter_spend_today_usd gauge",
                f'agentwriter_spend_today_usd{{kind="reserved_or_known"}} {spend["reserved_or_known_usd"]}',
                "# HELP agentwriter_spend_unknown_usd Provider spend with an unknown outcome today.",
                "# TYPE agentwriter_spend_unknown_usd gauge",
                f'agentwriter_spend_unknown_usd {spend["unknown_usd"]}',
                "# HELP agentwriter_spend_unknown_calls Calls with unknown spend today.",
                "# TYPE agentwriter_spend_unknown_calls gauge",
                f"agentwriter_spend_unknown_calls {int(spend['unknown_count'])}",
                "# HELP agentwriter_events_24h Durable events in the last 24 hours.",
                "# TYPE agentwriter_events_24h gauge",
                f"agentwriter_events_24h {int(snapshot['events_24h'])}",
            ]
        )
        return PlainTextResponse(
            "\n".join(lines) + "\n",
            media_type="text/plain; version=0.0.4; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    if WEB_ROOT.exists():
        app.mount("/assets", StaticFiles(directory=WEB_ROOT), name="assets")

        @app.get("/", include_in_schema=False)
        async def home():
            return FileResponse(WEB_ROOT / "index.html")

    return app
