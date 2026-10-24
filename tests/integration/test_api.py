"""HTTP acceptance against the real app, PostgreSQL and worker entry point."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentwriter.settings import Settings
from agentwriter.storage import Repository
from agentwriter.storage.pool import database_pool
from agentwriter.worker.runner import process_claim

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
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


async def enqueue(client, *, key: str = "api-key", **changes):
    response = await client.post(
        "/api/v1/runs", json=brief(**changes), headers={"Idempotency-Key": key}
    )
    assert response.status_code == 202, response.text
    return response.json()


async def run_one_fixture_job(url: str) -> None:
    settings = Settings(_env_file=None)
    async with database_pool(url, migrations_directory=MIGRATIONS) as pool:
        repository = Repository(pool, url, max_run_seconds=settings.max_run_seconds)
        await repository.worker_heartbeat("api-acceptance-worker")
        claim = await repository.claim_job("api-acceptance-worker")
        assert claim is not None
        await process_claim(repository, claim, settings)


async def test_create_observe_worker_article_edit_download_and_delete(
    api_client, acceptance_database: str
) -> None:
    client, app = api_client
    created = await enqueue(client)
    run_id = created["id"]
    assert created["state"] == "queued"
    assert created["events_url"].endswith(f"/{run_id}/events")

    duplicate = await client.post(
        "/api/v1/runs", json=brief(), headers={"Idempotency-Key": "api-key"}
    )
    assert duplicate.status_code == 202 and duplicate.json()["id"] == run_id
    conflict = await client.post(
        "/api/v1/runs",
        json=brief(topic="Write a short story about a different dragon"),
        headers={"Idempotency-Key": "api-key"},
    )
    assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_conflict"

    history = await client.get("/api/v1/runs")
    assert [row["id"] for row in history.json()["runs"]] == [run_id]

    repository = Repository(
        app.state.repository.pool, acceptance_database, max_run_seconds=600
    )
    assert not await repository.ready()
    await repository.worker_heartbeat("api-acceptance-worker")
    ready = await client.get("/ready")
    assert ready.status_code == 200 and ready.json()["status"] == "ready"
    assert (await client.get("/live")).json() == {"status": "alive"}

    await run_one_fixture_job(acceptance_database)
    status = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert status["state"] == "needs_review"
    assert status["current_revision"]
    # Progress counts accepted logical sections against the latest plan, not
    # every retained draft row from each review revision.
    assert status["progress"]["sections"] == 2
    assert status["progress"]["tasks"] == 2
    assert status["progress"]["sources"] == 1

    # Native SSE must replay committed UTF-8 events, honour Last-Event-ID and
    # terminate after the terminal event.
    lines: list[str] = []
    async with client.stream(
        "GET", f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": "2"}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        async for line in response.aiter_lines():
            if line:
                lines.append(line)
    ids = [int(line[4:]) for line in lines if line.startswith("id: ")]
    assert ids and min(ids) >= 3
    assert any(line == "event: completed" for line in lines)
    assert ids == sorted(set(ids))

    sources = (await client.get(f"/api/v1/runs/{run_id}/sources")).json()["sources"]
    assert len(sources) == 1 and sources[0]["provider"] == "fixture"
    article = (await client.get(f"/api/v1/runs/{run_id}/article")).json()
    assert article["review_status"] == "needs_review"
    assert article["markdown"].startswith("> Synthetic development draft")
    download = await client.get(
        f"/api/v1/runs/{run_id}/revisions/{article['id']}/download"
    )
    assert download.status_code == 200
    assert download.headers["etag"] == f'"{article["content_hash"]}"'
    assert download.text == article["markdown"]

    edited = await client.post(
        f"/api/v1/runs/{run_id}/revisions",
        json={"parent_revision": article["id"], "markdown": "# Edited\n\nMy own draft."},
    )
    assert edited.status_code == 201, edited.text
    edit = edited.json()
    assert edit["review_status"] == "unassessed" and edit["author_type"] == "human"
    assert edit["citation_map"]["editable_markdown"] == "# Edited\n\nMy own draft."
    stale = await client.post(
        f"/api/v1/runs/{run_id}/revisions",
        json={"parent_revision": article["id"], "markdown": "Stale edit"},
    )
    assert stale.status_code == 409 and stale.json()["code"] == "revision_conflict"
    revisions = (await client.get(f"/api/v1/runs/{run_id}/revisions")).json()["revisions"]
    assert len(revisions) == 2

    # Browser cannot remove CSRF or use another origin.
    missing = await client.post(
        "/api/v1/runs",
        json=brief(),
        headers={"Idempotency-Key": "no-csrf", "X-CSRF-Token": ""},
    )
    assert missing.status_code == 403 and missing.json()["code"] == "csrf_rejected"
    client.headers["Origin"] = "https://attacker.example"
    rejected = await client.post(
        "/api/v1/runs", json=brief(), headers={"Idempotency-Key": "bad-origin"}
    )
    assert rejected.status_code == 403 and rejected.json()["code"] == "origin_rejected"
    del client.headers["Origin"]

    # Simulate a second authenticated principal at the identity boundary. Local
    # development identity is loopback-only, so a second owner needs a new
    # persisted session rather than a forged cookie.
    original_owner = app.state.settings.local_owner_id
    original_cookies = dict(client.cookies)
    original_csrf = client.headers["X-CSRF-Token"]
    app.state.settings.local_owner_id = "another-local-user"
    second = await client.post("/api/v1/session")
    assert second.status_code == 200
    client.headers["X-CSRF-Token"] = second.json()["csrf_token"]
    try:
        hidden_paths = [
            f"/api/v1/runs/{run_id}",
            f"/api/v1/runs/{run_id}/sources",
            f"/api/v1/runs/{run_id}/article",
            f"/api/v1/runs/{run_id}/revisions",
            f"/api/v1/runs/{run_id}/revisions/{article['id']}/download",
            f"/api/v1/runs/{run_id}/events",
        ]
        for path in hidden_paths:
            hidden = await client.get(path)
            assert hidden.status_code == 404, (path, hidden.status_code, hidden.text)
        hidden_edit = await client.post(
            f"/api/v1/runs/{run_id}/revisions",
            json={"parent_revision": article["id"], "markdown": "# Not mine"},
        )
        assert hidden_edit.status_code == 404
        hidden_cancel = await client.post(f"/api/v1/runs/{run_id}/cancel")
        assert hidden_cancel.status_code == 404
        hidden_delete = await client.delete(f"/api/v1/runs/{run_id}")
        assert hidden_delete.status_code == 404
        assert (await client.get("/api/v1/runs")).json()["runs"] == []
    finally:
        app.state.settings.local_owner_id = original_owner
        client.cookies.clear()
        client.cookies.update(original_cookies)
        client.headers["X-CSRF-Token"] = original_csrf

    deleted = await client.delete(f"/api/v1/runs/{run_id}")
    assert deleted.status_code == 204
    assert (await client.get(f"/api/v1/runs/{run_id}")).status_code == 404
    assert (await client.get("/api/v1/runs")).json()["runs"] == []


async def test_cancel_invalid_cursor_body_limit_and_safe_errors(api_client) -> None:
    client, app = api_client
    created = await enqueue(client, key="cancel-key")
    run_id = created["id"]
    cancelled = await client.post(f"/api/v1/runs/{run_id}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"

    bad_cursor = await client.get(
        f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": "not-a-number"}
    )
    assert bad_cursor.status_code == 422 and bad_cursor.json()["code"] == "invalid_cursor"
    future = await client.get(f"/api/v1/runs/{run_id}/events?after=999")
    assert future.status_code == 409 and future.json()["code"] == "future_cursor"

    invalid = await client.post(
        "/api/v1/runs",
        json={"topic": "", "target_words": "many"},
        headers={"Idempotency-Key": "invalid"},
    )
    assert invalid.status_code == 422 and invalid.json()["code"] == "invalid_request"
    assert invalid.json()["correlation_id"]

    oversized = await client.post(
        "/api/v1/runs",
        content=b"{" + b"x" * 40_000,
        headers={"Idempotency-Key": "large", "Content-Type": "application/json"},
    )
    assert oversized.status_code == 413
    assert oversized.json()["code"] == "body_too_large"
    assert "x" * 100 not in oversized.text
    assert app.state.settings.max_request_bytes == 32_768


async def test_history_pagination_is_stable(api_client) -> None:
    client, _ = api_client
    topics = [
        "Write a short story about a dragon",
        "Write a short story about a knight",
        "Write a short story about a wizard",
    ]
    for index, topic in enumerate(topics):
        await enqueue(client, key=f"page-{index}", topic=topic)
    first = (await client.get("/api/v1/runs?limit=2")).json()
    assert len(first["runs"]) == 2 and first["next_cursor"]
    second = (await client.get(f"/api/v1/runs?limit=2&before={first['next_cursor']}")).json()
    assert len(second["runs"]) == 1
    assert {row["id"] for row in first["runs"]}.isdisjoint({row["id"] for row in second["runs"]})
    assert second["next_cursor"] is None



