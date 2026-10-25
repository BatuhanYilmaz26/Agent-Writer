"""On-demand discovery exercises owner scoping and database cooldowns."""

import pytest

pytestmark = pytest.mark.asyncio


async def test_discovery_snapshot_is_cached_owned_and_marks_previous_briefs(api_client):
    client, app = api_client
    assert (await client.get("/api/v1/topics")).json()["candidates"] == []
    first = await client.post("/api/v1/topics/refresh")
    assert first.status_code == 200
    snapshot = first.json()
    assert snapshot["fixture"] and len(snapshot["candidates"]) == 1
    topic = snapshot["candidates"][0]
    response = await client.post("/api/v1/runs", json={
        "topic": topic["title"], "reference_urls": [topic["url"]], "target_words": 350,
    }, headers={"Idempotency-Key": "discovered-topic"})
    assert response.status_code == 202
    cached = (await client.post("/api/v1/topics/refresh")).json()
    assert cached["retrieved_at"] == snapshot["retrieved_at"]
    assert cached["candidates"][0]["already_drafted"]
    assert not (await app.state.repository.topic_snapshot(app.state.settings.local_owner_id, refresh=True))[1]
    assert (await client.get("/api/v1/topics?days=0")).status_code == 422
    assert (await client.post("/api/v1/topics/refresh", headers={"X-CSRF-Token": ""})).status_code == 403
    app.state.settings.local_owner_id = "another-topic-owner"
    session = await client.post("/api/v1/session")
    client.headers["X-CSRF-Token"] = session.json()["csrf_token"]
    assert (await client.get("/api/v1/topics")).json()["candidates"] == []
