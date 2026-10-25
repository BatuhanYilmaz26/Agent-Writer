"""Real-socket SSE acceptance for heartbeat, reconnect, revocation and limits.

``httpx.ASGITransport`` buffers a response until its generator finishes, which
cannot exercise an infinite heartbeat stream. These checks run the real app in
an in-process Uvicorn server over a loopback socket.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit
from uuid import uuid4

import httpx
import pytest
import uvicorn

from agentwriter.main import create_app
from agentwriter.settings import Settings
from agentwriter.storage import Repository
from agentwriter.storage.pool import database_pool
from agentwriter.worker.runner import process_claim

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
pytestmark = pytest.mark.asyncio


def brief() -> dict[str, object]:
    return {
        "topic": "Write a short story about a dragon",
        "audience": "General readers",
        "tone": "clear",
        "language": "en",
        "target_words": 500,
        "research_mode": "required",
        "instructions": "",
    }


@asynccontextmanager
async def live_server(database_url: str) -> AsyncIterator[tuple[str, Any]]:
    parsed = urlsplit(database_url)
    query = dict(parse_qsl(parsed.query))
    query["application_name"] = f"sse-test-{uuid4().hex}"
    server_database_url = parsed._replace(query=urlencode(query, quote_via=quote)).geturl()
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=server_database_url,
        allowed_hosts=("127.0.0.1", "localhost"),
        max_user_nonterminal_runs=10,
        sse_heartbeat_seconds=1,
        sse_poll_seconds=0.1,
        max_observers_per_user=1,
    )
    app = create_app(settings)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        lifespan="on",
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    # The test owns the loop; installing process-wide signal handlers would
    # interfere with the pytest process.
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            if task.done():
                task.result()
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("Uvicorn did not start")
        socket = server.servers[0].sockets[0]
        port = socket.getsockname()[1]
        yield f"http://127.0.0.1:{port}", app
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()


async def start_session(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/session")
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]


async def enqueue(client: httpx.AsyncClient, key: str) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/runs", json=brief(), headers={"Idempotency-Key": key}
    )
    assert response.status_code == 202, response.text
    return response.json()


class SSEReader:
    """Incremental SSE line reader shared across assertions on one response."""

    def __init__(self, response: httpx.Response) -> None:
        self._iterator = response.aiter_lines().__aiter__()
        self.lines: list[str] = []

    async def until(self, predicate, *, timeout: float = 10) -> list[str]:
        async def consume() -> list[str]:
            while True:
                line = await self._iterator.__anext__()
                if not line:
                    continue
                self.lines.append(line)
                if predicate(line):
                    return self.lines

        return await asyncio.wait_for(consume(), timeout=timeout)


async def run_one_fixture_job(url: str) -> None:
    settings = Settings(_env_file=None)
    async with database_pool(url, migrations_directory=MIGRATIONS) as pool:
        repository = Repository(pool, url, max_run_seconds=settings.max_run_seconds)
        await repository.worker_heartbeat("sse-acceptance-worker")
        claim = await repository.claim_job("sse-acceptance-worker")
        assert claim is not None
        await process_claim(repository, claim, settings)


async def test_real_socket_heartbeat_then_terminal_reconnect(
    acceptance_database: str,
) -> None:
    async with live_server(acceptance_database) as (base_url, app):
        async with httpx.AsyncClient(base_url=base_url, timeout=15) as client:
            await start_session(client)
            created = await enqueue(client, "socket-heartbeat")
            run_id = created["id"]
            url = f"/api/v1/runs/{run_id}/events"

            async with client.stream("GET", url) as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                reader = SSEReader(response)
                lines = await reader.until(lambda line: line == ": heartbeat")
                assert any(line == "event: queued" for line in lines)
                assert any(line.startswith("id: 1") for line in lines)
                # A heartbeat is a comment, never a durable event id.
                heartbeat_at = lines.index(": heartbeat")
                assert not any(
                    line.startswith("id: ") for line in lines[heartbeat_at:]
                )
                # Polling releases its pooled connection; an observer must not
                # hold an idle-in-transaction session while waiting.
                # Other isolated schemas can be used by local previews at the
                # same time, so inspect this server's connections only.
                async with app.state.repository.pool.connection() as connection:
                    idle = await (
                        await connection.execute(
                            "SELECT count(*) AS count FROM pg_stat_activity "
                            "WHERE datname=current_database() AND state='idle in transaction' "
                            "AND application_name=current_setting('application_name')"
                        )
                    ).fetchone()
                assert idle["count"] == 0

            worker = asyncio.create_task(run_one_fixture_job(acceptance_database))
            async with client.stream("GET", url, headers={"Last-Event-ID": "1"}) as response:
                assert response.status_code == 200
                reader = SSEReader(response)
                lines = await reader.until(
                    lambda line: line == "event: completed", timeout=20
                )
                ids = [int(line[4:]) for line in lines if line.startswith("id: ")]
                assert ids == sorted(set(ids)) and min(ids) == 2
                assert lines[-1] == "event: completed"
            await worker

            status = (await client.get(f"/api/v1/runs/{run_id}")).json()
            assert status["state"] == "needs_review"
            # A terminal replay with nothing after the cursor is an ordinary 204.
            replay = await client.get(
                f"{url}?after={status['next_event_sequence']}", timeout=10
            )
            assert replay.status_code == 204
            assert replay.content == b""


async def test_real_socket_revocation_ends_stream_with_resync(
    acceptance_database: str,
) -> None:
    async with live_server(acceptance_database) as (base_url, _app):
        async with httpx.AsyncClient(base_url=base_url, timeout=15) as client:
            await start_session(client)
            created = await enqueue(client, "socket-revocation")
            url = f"/api/v1/runs/{created['id']}/events"
            async with client.stream("GET", url) as response:
                assert response.status_code == 200
                reader = SSEReader(response)
                await reader.until(lambda line: line == "event: queued")
                logged_out = await client.delete("/api/v1/session")
                assert logged_out.status_code == 204
                lines = await reader.until(
                    lambda line: line.startswith("data: ") and '"resync"' in line,
                    timeout=5,
                )
                assert any(line == "event: stream_error" for line in lines), lines
                assert any('"resync": true' in line for line in lines), lines
                assert any('"session_required"' in line for line in lines), lines


async def test_real_socket_observer_limit_and_disconnect_does_not_cancel_run(
    acceptance_database: str,
) -> None:
    async with live_server(acceptance_database) as (base_url, app):
        async with httpx.AsyncClient(base_url=base_url, timeout=15) as client:
            await start_session(client)
            created = await enqueue(client, "socket-observer")
            run_id = created["id"]
            url = f"/api/v1/runs/{run_id}/events"

            async with client.stream("GET", url) as first:
                assert first.status_code == 200
                reader = SSEReader(first)
                await reader.until(lambda line: line == "event: queued")
                assert app.state.observers[app.state.settings.local_owner_id] == 1
                second = await client.get(url)
                assert second.status_code == 429
                assert second.json()["code"] == "observer_limit"

                # A paused observer does not block the separately supervised
                # worker or the durable terminal transition.
                await run_one_fixture_job(acceptance_database)
                lines = await reader.until(
                    lambda line: line == "event: completed", timeout=20
                )
                assert lines[-1] == "event: completed"

            # Closing the browser stream must release the observer slot and
            # must not cancel durable work.
            for _ in range(50):
                if not app.state.observers.get(app.state.settings.local_owner_id):
                    break
                await asyncio.sleep(0.05)
            assert not app.state.observers.get(app.state.settings.local_owner_id)

            status = (await client.get(f"/api/v1/runs/{run_id}")).json()
            assert status["state"] == "needs_review"


async def test_simultaneous_streams_cannot_race_past_the_observer_cap(
    acceptance_database: str, monkeypatch,
) -> None:
    async with live_server(acceptance_database) as (base_url, app):
        async with httpx.AsyncClient(base_url=base_url, timeout=15) as client:
            await start_session(client)
            created = await enqueue(client, "simultaneous-observers")
            original = app.state.repository.list_events
            arrivals = 0
            both_reading = asyncio.Event()

            async def synchronized_events(*args, **kwargs):
                nonlocal arrivals
                rows = await original(*args, **kwargs)
                arrivals += 1
                if arrivals == 2:
                    both_reading.set()
                await asyncio.wait_for(both_reading.wait(), timeout=5)
                return rows

            monkeypatch.setattr(app.state.repository, "list_events", synchronized_events)
            url = f"/api/v1/runs/{created['id']}/events"
            responses = await asyncio.gather(*(
                client.send(client.build_request("GET", url), stream=True) for _ in range(2)
            ))
            try:
                assert sorted(response.status_code for response in responses) == [200, 429]
                assert app.state.observers[app.state.settings.local_owner_id] == 1
            finally:
                await asyncio.gather(*(response.aclose() for response in responses))
            for _ in range(50):
                if not app.state.observers:
                    break
                await asyncio.sleep(0.05)
            assert app.state.observers == {}
