"""Transactional owner-scoped persistence and leased job ownership.

All provider work happens outside short application transactions. A Claim's
dedicated connection owns both the session advisory lock and the graph saver;
it must not reconnect or be returned to a connection pool.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

TERMINAL_STATES = frozenset({"succeeded", "needs_review", "failed", "cancelled"})
NONTERMINAL_STATES = ("queued", "running", "retry_wait", "cancel_requested")
ADMISSION_LOCK = 1195461970


class StorageError(Exception):
    """Public-safe persistence/business error, never an upstream SQL message."""

    def __init__(self, code: str, message: str, status_code: int = 409):
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def canonical_hash(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def markdown_hash(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def _uuid(value: str | UUID) -> UUID:
    try:
        return UUID(str(value))
    except ValueError as error:
        raise StorageError("not_found", "Run or revision not found", 404) from error


def _public(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_public(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


@dataclass
class Claim:
    connection: psycopg.AsyncConnection[dict[str, Any]]
    run: dict[str, Any]
    worker_id: str
    generation: int
    lease_seconds: int
    advisory_key: int
    backend_pid: int

    @property
    def run_id(self) -> str:
        return str(self.run["id"])

    async def close(self) -> None:
        """Call only after graph tasks stop; closing releases the run lock."""
        await self.connection.close()


class Repository:
    def __init__(
        self,
        pool: AsyncConnectionPool[psycopg.AsyncConnection[dict[str, Any]]],
        database_url: str,
        *,
        max_user_runs: int = 2,
        max_global_runs: int = 100,
        max_run_seconds: int = 600,
        graph_version: str = "v1",
    ):
        self.pool = pool
        self.database_url = database_url
        self.max_user_runs = max_user_runs
        self.max_global_runs = max_global_runs
        self.max_run_seconds = max_run_seconds
        self.graph_version = graph_version

    async def _owned(
        self, connection: Any, owner: str, run_id: str | UUID, *, lock: bool = False
    ) -> dict:
        query = "SELECT * FROM runs WHERE id=%s AND owner_id=%s AND deleted_at IS NULL"
        if lock:
            query += " FOR UPDATE"
        row = await (await connection.execute(query, (_uuid(run_id), owner))).fetchone()
        if row is None:
            raise StorageError("not_found", "Run not found", 404)
        return row

    async def _event(
        self, connection: Any, run_id: str | UUID, event_type: str, payload: dict, event_key: str
    ) -> dict:
        # Lock first: duplicate identity checks and sequence allocation have the
        # same serialization order as the state transaction that publishes them.
        await connection.execute("SELECT id FROM runs WHERE id=%s FOR UPDATE", (_uuid(run_id),))
        existing = await (
            await connection.execute(
                "SELECT * FROM run_events WHERE run_id=%s AND event_key=%s",
                (_uuid(run_id), event_key),
            )
        ).fetchone()
        if existing:
            if existing["event_type"] != event_type or existing["payload"] != payload:
                raise StorageError("event_conflict", "An event identity has conflicting contents")
            return existing
        row = await (
            await connection.execute(
                "UPDATE runs SET next_event_sequence=next_event_sequence+1,updated_at=clock_timestamp() "
                "WHERE id=%s RETURNING next_event_sequence",
                (_uuid(run_id),),
            )
        ).fetchone()
        return await (
            await connection.execute(
                "INSERT INTO run_events(run_id,sequence,event_key,event_type,payload) "
                "VALUES (%s,%s,%s,%s,%s) RETURNING *",
                (_uuid(run_id), row["next_event_sequence"], event_key, event_type, Jsonb(payload)),
            )
        ).fetchone()

    async def create_run(self, owner: str, request: dict, idempotency_key: str) -> dict:
        if not 1 <= len(idempotency_key) <= 200 or not idempotency_key.isascii():
            raise StorageError(
                "invalid_idempotency_key", "Use an ASCII idempotency key of 1–200 characters", 422
            )
        if not owner or len(owner) > 255:
            raise StorageError("invalid_principal", "Invalid authenticated principal", 401)
        digest = canonical_hash(request)
        async with self.pool.connection() as connection, connection.transaction():
            # Admission is deliberately serialized initially: user/global caps
            # must not be defeated by concurrent submissions on several APIs.
            await connection.execute("SELECT pg_advisory_xact_lock(%s)", (ADMISSION_LOCK,))
            await connection.execute(
                "INSERT INTO users(id) VALUES (%s) ON CONFLICT DO NOTHING", (owner,)
            )
            await connection.execute(
                "DELETE FROM idempotency_keys WHERE owner_id=%s AND key=%s AND expires_at<=clock_timestamp()",
                (owner, idempotency_key),
            )
            existing = await (
                await connection.execute(
                    "SELECT i.request_hash,r.* FROM idempotency_keys i JOIN runs r ON r.id=i.run_id "
                    "WHERE i.owner_id=%s AND i.key=%s",
                    (owner, idempotency_key),
                )
            ).fetchone()
            if existing:
                if existing["deleted_at"] is not None:
                    raise StorageError(
                        "run_deleted", "This idempotency key belongs to a deleted run", 410
                    )
                if existing["request_hash"] != digest:
                    raise StorageError(
                        "idempotency_conflict", "This key was used for a different writing brief"
                    )
                return _public(existing)
            counts = await (
                await connection.execute(
                    "SELECT count(*) AS total,count(*) FILTER (WHERE owner_id=%s) AS owned "
                    "FROM runs WHERE state=ANY(%s)",
                    (owner, list(NONTERMINAL_STATES)),
                )
            ).fetchone()
            assert counts is not None  # An aggregate SELECT always returns one row.
            if counts["total"] >= self.max_global_runs or counts["owned"] >= self.max_user_runs:
                raise StorageError(
                    "queue_full", "The writing queue is full. Try again after a run finishes.", 429
                )
            run_id = uuid4()
            row = await (
                await connection.execute(
                    "INSERT INTO runs(id,owner_id,request,request_hash,deadline_at,graph_version) "
                    "VALUES (%s,%s,%s,%s,clock_timestamp()+(%s*interval '1 second'),%s) RETURNING *",
                    (
                        run_id,
                        owner,
                        Jsonb(request),
                        digest,
                        self.max_run_seconds,
                        self.graph_version,
                    ),
                )
            ).fetchone()
            await connection.execute("INSERT INTO jobs(run_id) VALUES (%s)", (run_id,))
            await connection.execute(
                "INSERT INTO idempotency_keys(owner_id,key,request_hash,run_id,expires_at) "
                "VALUES (%s,%s,%s,%s,clock_timestamp()+interval '24 hours')",
                (owner, idempotency_key, digest, run_id),
            )
            await self._event(connection, run_id, "queued", {"state": "queued"}, "queued")
            assert row is not None
            row["next_event_sequence"] = 1
            return _public(row)

    async def get_run(self, owner: str, run_id: str) -> dict:
        async with self.pool.connection() as connection:
            return _public(await self._owned(connection, owner, run_id))

    async def list_runs(
        self, owner: str, *, limit: int = 20, before: str | None = None
    ) -> list[dict]:
        if not 1 <= limit <= 100:
            raise StorageError("invalid_limit", "Page size must be between 1 and 100", 422)
        async with self.pool.connection() as connection:
            params: list[Any] = [owner]
            query = "SELECT * FROM runs WHERE owner_id=%s AND deleted_at IS NULL"
            if before:
                cursor = await self._owned(connection, owner, before)
                query += " AND (accepted_at,id)<(%s,%s)"
                params.extend([cursor["accepted_at"], cursor["id"]])
            query += " ORDER BY accepted_at DESC,id DESC LIMIT %s"
            params.append(limit)
            return _public(await (await connection.execute(query, params)).fetchall())

    async def list_events(
        self, owner: str, run_id: str, *, after: int = 0, limit: int = 100
    ) -> list[dict]:
        if after < 0 or not 1 <= limit <= 500:
            raise StorageError("invalid_cursor", "Invalid event cursor or page size", 422)
        async with self.pool.connection() as connection, connection.transaction():
            run = await self._owned(connection, owner, run_id, lock=True)
            if after > run["next_event_sequence"]:
                raise StorageError("future_cursor", "Event cursor is ahead of this run", 409)
            first = await (
                await connection.execute(
                    "SELECT min(sequence) AS sequence FROM run_events WHERE run_id=%s",
                    (_uuid(run_id),),
                )
            ).fetchone()
            assert first is not None
            if after and (
                (first["sequence"] is None and after < run["next_event_sequence"])
                or (first["sequence"] is not None and after < first["sequence"] - 1)
            ):
                raise StorageError(
                    "expired_cursor", "Event history expired. Reload the run status.", 410
                )
            rows = await (
                await connection.execute(
                    "SELECT * FROM run_events WHERE run_id=%s AND sequence>%s ORDER BY sequence LIMIT %s",
                    (_uuid(run_id), after, limit),
                )
            ).fetchall()
            return _public(rows)

    async def cancel_run(self, owner: str, run_id: str) -> dict:
        async with self.pool.connection() as connection, connection.transaction():
            run = await self._owned(connection, owner, run_id, lock=True)
            if run["state"] in TERMINAL_STATES or run["state"] == "cancel_requested":
                return _public(run)
            state = "cancelled" if run["state"] in {"queued", "retry_wait"} else "cancel_requested"
            updated = await (
                await connection.execute(
                    "UPDATE runs SET state=%s,updated_at=clock_timestamp() WHERE id=%s RETURNING *",
                    (state, _uuid(run_id)),
                )
            ).fetchone()
            assert updated is not None
            run = updated
            await self._event(connection, run_id, state, {"state": state}, state)
            return _public(run)

    async def delete_run(self, owner: str, run_id: str) -> None:
        async with self.pool.connection() as connection, connection.transaction():
            run = await self._owned(connection, owner, run_id, lock=True)
            state = run["state"]
            if state in {"queued", "retry_wait"}:
                state = "cancelled"
            elif state not in TERMINAL_STATES:
                state = "cancel_requested"
            await connection.execute(
                "UPDATE runs SET state=%s,deleted_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=%s",
                (state, _uuid(run_id)),
            )
            await self._event(connection, run_id, "deleted", {"state": state}, "deleted")

    async def sources(
        self, owner: str, run_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        if not 1 <= limit <= 100 or offset < 0:
            raise StorageError("invalid_page", "Invalid source page", 422)
        async with self.pool.connection() as connection:
            await self._owned(connection, owner, run_id)
            rows = await (
                await connection.execute(
                    "SELECT source_data FROM sources WHERE run_id=%s ORDER BY source_id LIMIT %s OFFSET %s",
                    (_uuid(run_id), limit, offset),
                )
            ).fetchall()
            return [row["source_data"] for row in rows]

    async def get_article(self, owner: str, run_id: str, revision_id: str | None = None) -> dict:
        async with self.pool.connection() as connection:
            run = await self._owned(connection, owner, run_id)
            selected = _uuid(revision_id) if revision_id else run["current_revision"]
            row = await (
                await connection.execute(
                    "SELECT * FROM article_revisions WHERE run_id=%s AND id=%s",
                    (_uuid(run_id), selected),
                )
            ).fetchone()
            if row is None:
                raise StorageError("article_not_found", "No article revision is available", 404)
            return _public(row)

    async def list_revisions(self, owner: str, run_id: str, *, limit: int = 50) -> list[dict]:
        if not 1 <= limit <= 100:
            raise StorageError("invalid_limit", "Page size must be between 1 and 100", 422)
        async with self.pool.connection() as connection:
            await self._owned(connection, owner, run_id)
            return _public(
                await (
                    await connection.execute(
                        "SELECT id,parent_revision,content_hash,review_status,author_type,created_at "
                        "FROM article_revisions WHERE run_id=%s ORDER BY created_at DESC,id DESC LIMIT %s",
                        (_uuid(run_id), limit),
                    )
                ).fetchall()
            )

    async def edit_article(
        self, owner: str, run_id: str, parent_revision: str, markdown: str,
        *, citation_map: dict | None = None,
    ) -> dict:
        if not markdown.strip() or len(markdown) > 200000:
            raise StorageError(
                "invalid_markdown", "Article text must contain 1–200,000 characters", 422
            )
        async with self.pool.connection() as connection, connection.transaction():
            run = await self._owned(connection, owner, run_id, lock=True)
            if run["state"] not in TERMINAL_STATES or run["current_revision"] != _uuid(
                parent_revision
            ):
                raise StorageError(
                    "revision_conflict", "The article changed. Reload before saving your edit."
                )
            parent = await (
                await connection.execute(
                    "SELECT citation_map FROM article_revisions WHERE id=%s",
                    (_uuid(parent_revision),),
                )
            ).fetchone()
            assert parent is not None  # Protected by current_revision's foreign key.
            revision_id = uuid4()
            row = await (
                await connection.execute(
                    "INSERT INTO article_revisions(id,run_id,parent_revision,markdown,content_hash,review_status,author_type,citation_map) "
                    "VALUES (%s,%s,%s,%s,%s,'unassessed','human',%s) RETURNING *",
                    (
                        revision_id,
                        _uuid(run_id),
                        _uuid(parent_revision),
                        markdown,
                        markdown_hash(markdown),
                        Jsonb(parent["citation_map"] if citation_map is None else citation_map),
                    ),
                )
            ).fetchone()
            await connection.execute(
                "UPDATE runs SET current_revision=%s WHERE id=%s", (revision_id, _uuid(run_id))
            )
            await self._event(
                connection,
                run_id,
                "article_edited",
                {"revision_id": str(revision_id), "review_status": "unassessed"},
                f"edit:{revision_id}",
            )
            return _public(row)

    async def _fence(
        self,
        connection: Any,
        claim: Claim,
        *,
        allow_cancel: bool = False,
        allow_deadline: bool = False,
    ) -> dict:
        if claim.connection.closed:
            raise StorageError("lease_lost", "Execution ownership was lost")
        row = await (
            await connection.execute(
                "SELECT r.*,j.lease_owner,j.lease_expires_at,j.generation,"
                "j.lease_expires_at>clock_timestamp() AS lease_valid,"
                "r.deadline_at>clock_timestamp() AS deadline_valid "
                "FROM runs r JOIN jobs j ON j.run_id=r.id WHERE r.id=%s FOR UPDATE OF r",
                (_uuid(claim.run_id),),
            )
        ).fetchone()
        if (
            row is None
            or row["lease_owner"] != claim.worker_id
            or row["generation"] != claim.generation
            or not row["lease_valid"]
        ):
            raise StorageError("lease_lost", "Execution ownership was lost")
        unsigned_key = claim.advisory_key & ((1 << 64) - 1)
        lock = await (
            await connection.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND pid=%s "
                "AND classid=%s::oid AND objid=%s::oid AND objsubid=1 AND granted) AS held",
                (claim.backend_pid, unsigned_key >> 32, unsigned_key & ((1 << 32) - 1)),
            )
        ).fetchone()
        if not lock["held"]:
            raise StorageError("lease_lost", "Execution database session was lost")
        allowed = {"running", "cancel_requested"} if allow_cancel else {"running"}
        if row["state"] not in allowed or (row["deleted_at"] is not None and not allow_cancel):
            raise StorageError("run_cancelled", "This run is no longer accepting generated work")
        if not row["deadline_valid"] and not allow_deadline:
            raise StorageError("deadline_exceeded", "The run deadline has expired")
        return row

    async def claim_job(self, worker_id: str, *, lease_seconds: int = 90) -> Claim | None:
        if not worker_id or len(worker_id) > 255 or not 1 <= lease_seconds <= 3600:
            raise ValueError("Invalid worker identity or lease duration")
        await self.expire_waiting()
        connection = await psycopg.AsyncConnection.connect(
            self.database_url,
            autocommit=True,
            row_factory=dict_row,
            connect_timeout=5,
            # Session locks require a persistent PostgreSQL session, not a
            # transaction-pooled proxy or transparent reconnecting wrapper.
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
        )
        claimed = False
        try:
            async with connection.transaction():
                candidate = await (
                    await connection.execute(
                        "SELECT r.*,j.attempt,j.max_attempts FROM jobs j JOIN runs r ON r.id=j.run_id "
                        "WHERE r.state=ANY(%s) AND j.due_at<=clock_timestamp() "
                        "AND (j.lease_owner IS NULL OR j.lease_expires_at<=clock_timestamp()) "
                        "ORDER BY j.due_at,r.accepted_at,r.id LIMIT 1 FOR UPDATE OF r SKIP LOCKED",
                        (list(NONTERMINAL_STATES),),
                    )
                ).fetchone()
                if candidate is None:
                    return None
                run_id = candidate["id"]
                lock_key = int.from_bytes(
                    hashlib.sha256(str(run_id).encode()).digest()[:8], "big", signed=True
                )
                locked = await (
                    await connection.execute(
                        "SELECT pg_try_advisory_lock(%s) AS acquired", (lock_key,)
                    )
                ).fetchone()
                assert locked is not None
                if not locked["acquired"]:
                    return None
                if candidate["state"] == "cancel_requested" or candidate["deleted_at"] is not None:
                    await connection.execute(
                        "UPDATE runs SET state='cancelled' WHERE id=%s", (run_id,)
                    )
                    await self._event(
                        connection, run_id, "cancelled", {"state": "cancelled"}, "cancelled"
                    )
                    return None
                expired = await (
                    await connection.execute(
                        "SELECT deadline_at<=clock_timestamp() AS expired FROM runs WHERE id=%s",
                        (run_id,),
                    )
                ).fetchone()
                assert expired is not None
                error = None
                if expired["expired"]:
                    error = "deadline_exceeded"
                elif candidate["attempt"] >= candidate["max_attempts"]:
                    error = "attempts_exhausted"
                elif candidate["graph_version"] != self.graph_version:
                    error = "graph_version_mismatch"
                if error:
                    await connection.execute(
                        "UPDATE runs SET state='failed',error_code=%s,error_message=%s WHERE id=%s",
                        (error, "This run could not be resumed. Start a new run.", run_id),
                    )
                    await self._event(
                        connection, run_id, "failed", {"state": "failed", "code": error}, "terminal"
                    )
                    return None
                lease = await (
                    await connection.execute(
                        "UPDATE jobs SET lease_owner=%s,lease_expires_at=clock_timestamp()+(%s*interval '1 second'),"
                        "generation=generation+1,attempt=attempt+1 WHERE run_id=%s RETURNING generation",
                        (worker_id, lease_seconds, run_id),
                    )
                ).fetchone()
                assert lease is not None
                row = await (
                    await connection.execute(
                        "UPDATE runs SET state='running',updated_at=clock_timestamp() WHERE id=%s RETURNING *",
                        (run_id,),
                    )
                ).fetchone()
                await self._event(
                    connection,
                    run_id,
                    "running",
                    {"state": "running", "attempt": candidate["attempt"] + 1},
                    f"running:{lease['generation']}",
                )
            assert row is not None
            row["attempt"] = candidate["attempt"] + 1
            row["resumed"] = candidate["state"] in {"running", "retry_wait"}
            claimed = True
            return Claim(
                connection,
                _public(row),
                worker_id,
                lease["generation"],
                lease_seconds,
                lock_key,
                connection.info.backend_pid,
            )
        finally:
            if not claimed:
                await connection.close()

    async def expire_waiting(self, *, limit: int = 100) -> int:
        """Expire queued/retry work even when its next retry is not due yet."""
        async with self.pool.connection() as connection, connection.transaction():
            rows = await (
                await connection.execute(
                    "SELECT id FROM runs WHERE state IN ('queued','retry_wait') "
                    "AND deadline_at<=clock_timestamp() ORDER BY deadline_at LIMIT %s FOR UPDATE SKIP LOCKED",
                    (limit,),
                )
            ).fetchall()
            for row in rows:
                await connection.execute(
                    "UPDATE runs SET state='failed',error_code='deadline_exceeded',"
                    "error_message='The run deadline expired before execution completed.' WHERE id=%s",
                    (row["id"],),
                )
                await self._event(
                    connection,
                    row["id"],
                    "failed",
                    {"state": "failed", "code": "deadline_exceeded"},
                    "terminal",
                )
            return len(rows)

    async def heartbeat(self, claim: Claim) -> bool:
        async with self.pool.connection() as connection, connection.transaction():
            try:
                await self._fence(connection, claim, allow_cancel=True, allow_deadline=True)
            except StorageError:
                return False
            await connection.execute(
                "UPDATE jobs SET lease_expires_at=clock_timestamp()+(%s*interval '1 second') WHERE run_id=%s",
                (claim.lease_seconds, _uuid(claim.run_id)),
            )
            return True

    async def check_active(self, claim: Claim) -> None:
        """Fence before a worker node/dispatch; returns no mutable run object."""
        async with self.pool.connection() as connection, connection.transaction():
            await self._fence(connection, claim)

    async def worker_heartbeat(self, worker_id: str, *, draining: bool = False) -> None:
        async with self.pool.connection() as connection:
            await connection.execute(
                "INSERT INTO worker_heartbeats(id,graph_version,draining) VALUES (%s,%s,%s) "
                "ON CONFLICT(id) DO UPDATE SET graph_version=EXCLUDED.graph_version,draining=EXCLUDED.draining,last_seen=clock_timestamp()",
                (worker_id, self.graph_version, draining),
            )

    async def ready(self, *, require_worker: bool = True, freshness_seconds: int = 60) -> bool:
        async with self.pool.connection() as connection:
            await connection.execute("SELECT 1 FROM schema_migrations LIMIT 1")
            if not require_worker:
                return True
            row = await (
                await connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM worker_heartbeats WHERE NOT draining AND graph_version=%s "
                    "AND last_seen>clock_timestamp()-(%s*interval '1 second')) AS fresh",
                    (self.graph_version, freshness_seconds),
                )
            ).fetchone()
            assert row is not None
            return bool(row["fresh"])

    async def append_event(
        self, claim: Claim, event_type: str, payload: dict, *, event_key: str | None = None
    ) -> dict:
        async with self.pool.connection() as connection, connection.transaction():
            await self._fence(connection, claim)
            return _public(
                await self._event(
                    connection,
                    claim.run_id,
                    event_type,
                    payload,
                    event_key or f"{event_type}:{canonical_hash(payload)}",
                )
            )

    async def save_plan(self, claim: Claim, plan: dict, *, revision: int = 0) -> None:
        async with self.pool.connection() as connection, connection.transaction():
            await self._fence(connection, claim)
            row = await (
                await connection.execute(
                    "SELECT plan FROM plans WHERE run_id=%s AND revision=%s",
                    (_uuid(claim.run_id), revision),
                )
            ).fetchone()
            if row:
                if row["plan"] != plan:
                    raise StorageError("plan_conflict", "A committed plan has conflicting contents")
                return
            await connection.execute(
                "INSERT INTO plans(run_id,revision,plan) VALUES (%s,%s,%s)",
                (_uuid(claim.run_id), revision, Jsonb(plan)),
            )

    async def save_sources(
        self, claim: Claim, sources: list[dict], excerpts: list[dict] | None = None
    ) -> None:
        async with self.pool.connection() as connection, connection.transaction():
            await self._fence(connection, claim)
            for source in sources:
                source_id = str(source.get("id") or source.get("source_id"))
                if source_id == "None":
                    raise StorageError("invalid_source", "Each source needs an identity", 422)
                existing = await (
                    await connection.execute(
                        "SELECT source_data FROM sources WHERE run_id=%s AND source_id=%s",
                        (_uuid(claim.run_id), source_id),
                    )
                ).fetchone()
                if existing:
                    if existing["source_data"] != source:
                        raise StorageError(
                            "source_conflict", "A source identity has conflicting contents"
                        )
                else:
                    await connection.execute(
                        "INSERT INTO sources(run_id,source_id,source_data) VALUES (%s,%s,%s)",
                        (_uuid(claim.run_id), source_id, Jsonb(source)),
                    )
            for excerpt in excerpts or []:
                existing = await (
                    await connection.execute(
                        "SELECT excerpt_data FROM evidence_excerpts WHERE run_id=%s AND id=%s",
                        (_uuid(claim.run_id), excerpt["id"]),
                    )
                ).fetchone()
                if existing:
                    if existing["excerpt_data"] != excerpt:
                        raise StorageError(
                            "excerpt_conflict", "An excerpt identity has conflicting contents"
                        )
                else:
                    await connection.execute(
                        "INSERT INTO evidence_excerpts(run_id,id,source_id,excerpt_data) VALUES (%s,%s,%s,%s)",
                        (_uuid(claim.run_id), excerpt["id"], excerpt["source_id"], Jsonb(excerpt)),
                    )

    async def save_section(self, claim: Claim, task_id: str, revision: int, content: dict) -> None:
        async with self.pool.connection() as connection, connection.transaction():
            await self._fence(connection, claim)
            digest = canonical_hash(content)
            existing = await (
                await connection.execute(
                    "SELECT content_hash FROM section_drafts WHERE run_id=%s AND task_id=%s AND revision=%s",
                    (_uuid(claim.run_id), task_id, revision),
                )
            ).fetchone()
            if existing:
                if existing["content_hash"] != digest:
                    raise StorageError(
                        "section_conflict", "A committed section identity has conflicting contents"
                    )
                return
            await connection.execute(
                "INSERT INTO section_drafts(run_id,task_id,revision,content,content_hash) VALUES (%s,%s,%s,%s,%s)",
                (_uuid(claim.run_id), task_id, revision, Jsonb(content), digest),
            )
            await self._event(
                connection,
                claim.run_id,
                "section_completed",
                {"task_id": task_id, "revision": revision},
                f"section:{task_id}:{revision}",
            )

    async def get_sections(self, run_id: str) -> list[dict]:
        """Internal worker-only operation; never expose without owner checks."""
        async with self.pool.connection() as connection:
            return _public(
                await (
                    await connection.execute(
                        "SELECT * FROM section_drafts WHERE run_id=%s ORDER BY task_id,revision",
                        (_uuid(run_id),),
                    )
                ).fetchall()
            )

    async def finalize(
        self,
        claim: Claim,
        markdown: str,
        *,
        review_status: str = "needs_review",
        issues: list[dict] | None = None,
        citation_map: dict | None = None,
    ) -> dict:
        if (
            review_status not in {"approved", "needs_review", "failed"}
            or not markdown.strip()
            or len(markdown) > 200000
        ):
            raise StorageError("invalid_article", "Invalid article output", 422)
        state = {"approved": "succeeded", "needs_review": "needs_review", "failed": "failed"}[
            review_status
        ]
        async with self.pool.connection() as connection, connection.transaction():
            await self._fence(connection, claim)
            revision_id = uuid4()
            row = await (
                await connection.execute(
                    "INSERT INTO article_revisions(id,run_id,markdown,content_hash,review_status,issues,citation_map,author_type) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,'model') RETURNING *",
                    (
                        revision_id,
                        _uuid(claim.run_id),
                        markdown,
                        markdown_hash(markdown),
                        review_status,
                        Jsonb(issues or []),
                        Jsonb(citation_map or {}),
                    ),
                )
            ).fetchone()
            await connection.execute(
                "UPDATE runs SET state=%s,current_revision=%s,updated_at=clock_timestamp() WHERE id=%s",
                (state, revision_id, _uuid(claim.run_id)),
            )
            await self._event(
                connection,
                claim.run_id,
                "completed",
                {"state": state, "revision_id": str(revision_id), "review_status": review_status},
                "terminal",
            )
            await connection.execute(
                "UPDATE jobs SET lease_owner=NULL,lease_expires_at=NULL WHERE run_id=%s",
                (_uuid(claim.run_id),),
            )
            return _public(row)

    async def fail_job(self, claim: Claim, code: str, message: str) -> None:
        async with self.pool.connection() as connection, connection.transaction():
            run = await self._fence(connection, claim, allow_cancel=True, allow_deadline=True)
            state = (
                "cancelled"
                if run["state"] == "cancel_requested" or run["deleted_at"] is not None
                else "failed"
            )
            await connection.execute(
                "UPDATE runs SET state=%s,error_code=%s,error_message=%s,updated_at=clock_timestamp() WHERE id=%s",
                (state, code, message, _uuid(claim.run_id)),
            )
            await self._event(
                connection, claim.run_id, state, {"state": state, "code": code}, "terminal"
            )
            await connection.execute(
                "UPDATE jobs SET lease_owner=NULL,lease_expires_at=NULL WHERE run_id=%s",
                (_uuid(claim.run_id),),
            )

    async def retry_job(self, claim: Claim, code: str, delay_seconds: float) -> None:
        if not 0 <= delay_seconds <= 86400:
            raise ValueError("Retry delay must be bounded")
        async with self.pool.connection() as connection, connection.transaction():
            await self._fence(connection, claim)
            await connection.execute(
                "UPDATE runs SET state='retry_wait',error_code=%s,updated_at=clock_timestamp() WHERE id=%s",
                (code, _uuid(claim.run_id)),
            )
            await connection.execute(
                "UPDATE jobs SET due_at=clock_timestamp()+(%s*interval '1 second'),lease_owner=NULL,lease_expires_at=NULL WHERE run_id=%s",
                (delay_seconds, _uuid(claim.run_id)),
            )
            await self._event(
                connection,
                claim.run_id,
                "retry_wait",
                {"state": "retry_wait", "code": code, "delay_seconds": delay_seconds},
                f"retry:{claim.generation}",
            )
