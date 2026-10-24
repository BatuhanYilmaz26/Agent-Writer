"""Browser-session, metrics and committed-progress acceptance against PostgreSQL."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import SecretStr

from agentwriter.api.security import SESSION_COOKIE

pytestmark = pytest.mark.asyncio


def brief(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "topic": "Write a short story about a dragon",
        "audience": "General readers",
        "tone": "clear",
        "language": "en",
        "target_words": 500,
        "research_mode": "required",
        "instructions": "",
    }
    payload.update(changes)
    return payload


async def enqueue(client, *, key: str = "session-key"):
    response = await client.post(
        "/api/v1/runs", json=brief(), headers={"Idempotency-Key": key}
    )
    assert response.status_code == 202, response.text
    return response.json()


async def test_session_is_hashed_refreshable_and_revocable(api_client) -> None:
    client, app = api_client
    current = await client.get("/api/v1/session")
    assert current.status_code == 200
    assert current.json()["csrf_token"] == client.headers["X-CSRF-Token"]

    raw = client.cookies.get(SESSION_COOKIE)
    assert raw and len(raw) >= 20
    async with app.state.repository.pool.connection() as connection:
        rows = await (
            await connection.execute("SELECT token_hash,csrf_hash,owner_id FROM sessions")
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["token_hash"] == hashlib.sha256(raw.encode()).hexdigest()
    assert raw not in {rows[0]["token_hash"], rows[0]["csrf_hash"]}
    assert rows[0]["owner_id"] == app.state.settings.local_owner_id

    missing = await client.post(
        "/api/v1/runs",
        json=brief(),
        headers={"Idempotency-Key": "missing-csrf", "X-CSRF-Token": ""},
    )
    assert missing.status_code == 403 and missing.json()["code"] == "csrf_rejected"

    logged_out = await client.delete("/api/v1/session")
    assert logged_out.status_code == 204
    assert (await client.get("/api/v1/session")).status_code == 401
    async with app.state.repository.pool.connection() as connection:
        row = await (
            await connection.execute("SELECT revoked_at FROM sessions LIMIT 1")
        ).fetchone()
    assert row["revoked_at"] is not None

    # A fresh login produces a new persisted session rather than reusing state.
    fresh = await client.post("/api/v1/session")
    assert fresh.status_code == 200
    assert (await client.get("/api/v1/session")).status_code == 200


async def test_expired_session_cannot_read_or_mutate(api_client) -> None:
    client, app = api_client
    async with app.state.repository.pool.connection() as connection:
        await connection.execute(
            "UPDATE sessions SET expires_at=clock_timestamp()-interval '1 second'"
        )
    assert (await client.get("/api/v1/runs")).status_code == 401
    assert (
        await client.post(
            "/api/v1/runs", json=brief(), headers={"Idempotency-Key": "expired"}
        )
    ).status_code == 401


async def test_terminal_replay_returns_204_and_status_has_progress(api_client) -> None:
    client, app = api_client
    created = await enqueue(client, key="terminal-204")
    run_id = created["id"]
    cancelled = await client.post(f"/api/v1/runs/{run_id}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
    status = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert status["progress"] == {
        "sections": 0,
        "tasks": 0,
        "sources": 0,
        "excerpts": 0,
        "unsupported_claims": 0,
    }
    assert status["research_limitations"] == []
    replay = await client.get(
        f"/api/v1/runs/{run_id}/events?after={status['next_event_sequence']}"
    )
    assert replay.status_code == 204

    # An observer cap is enforced before a second stream starts.
    app.state.observers[app.state.settings.local_owner_id] = (
        app.state.settings.max_observers_per_user
    )
    try:
        limited = await client.get(f"/api/v1/runs/{run_id}/events")
        assert limited.status_code == 429 and limited.json()["code"] == "observer_limit"
    finally:
        app.state.observers.clear()


async def test_metrics_are_token_protected_and_low_cardinality(api_client) -> None:
    client, app = api_client
    created = await enqueue(client, key="metrics-run")
    app.state.settings.metrics_token = SecretStr("metrics-test-token")
    try:
        assert (await client.get("/metrics")).status_code == 401
        assert (
            await client.get("/metrics", headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
        response = await client.get(
            "/metrics", headers={"Authorization": "Bearer metrics-test-token"}
        )
    finally:
        app.state.settings.metrics_token = None
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert 'agentwriter_runs{state="queued"} 1' in body
    assert "agentwriter_queue_runs" in body
    assert "agentwriter_workers" in body
    assert created["id"] not in body
    assert "metrics-test-token" not in body


async def test_loopback_metrics_work_without_a_token(api_client) -> None:
    client, _ = api_client
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "agentwriter_runs" in response.text


async def test_session_cookie_flags_and_trusted_host_boundary(api_client) -> None:
    client, _ = api_client
    response = await client.post("/api/v1/session")
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "path=/" in cookie
    assert "secure" not in cookie  # loopback HTTP development

    rejected = await client.get("/api/v1/runs", headers={"Host": "attacker.example"})
    assert rejected.status_code == 400


async def test_explicit_cors_allowlist_is_exact(acceptance_database: str) -> None:
    import httpx

    from agentwriter.main import create_app
    from agentwriter.settings import Settings

    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=acceptance_database,
        allowed_hosts=("127.0.0.1", "localhost", "testserver"),
        allowed_origins=("https://app.example",),
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            allowed = await client.options(
                "/api/v1/runs",
                headers={
                    "Origin": "https://app.example",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type,x-csrf-token",
                },
            )
            assert allowed.status_code == 200
            assert allowed.headers["access-control-allow-origin"] == "https://app.example"
            denied = await client.options(
                "/api/v1/runs",
                headers={
                    "Origin": "https://attacker.example",
                    "Access-Control-Request-Method": "POST",
                },
            )
            assert denied.status_code == 400
            assert "access-control-allow-origin" not in denied.headers
