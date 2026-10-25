"""Durable, owner-scoped assessments of immutable saved human revisions."""

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from conftest import scoped_database
from psycopg.types.json import Jsonb

from fieldnote.providers.openrouter import OpenRouterGateway
from fieldnote.services.budgets import BudgetService
from fieldnote.settings import Settings
from fieldnote.storage import Repository
from fieldnote.storage.migrate import migrate
from fieldnote.storage.pool import database_pool
from fieldnote.worker.runner import ProviderRuntime, process_claim

pytestmark = pytest.mark.asyncio
BODY = "# The village clock\n\n" + "The dragon repairs a clock beside the forest. " * 60


async def saved_edit(client, app, dsn, **changes):
    request = {"topic": "Write a short story about a dragon", "research_mode": "off", "target_words": 500, **changes}
    created = await client.post("/api/v1/runs", json=request,
                                headers={"Idempotency-Key": uuid4().hex})
    assert created.status_code == 202
    run = created.json()
    claim = await app.state.repository.claim_job("original-writer")
    assert claim is not None
    await process_claim(app.state.repository, claim, Settings(_env_file=None, database_url=dsn, max_review_rounds=0))
    generated = (await client.get(f"/api/v1/runs/{run['id']}/article")).json()
    edited = await client.post(f"/api/v1/runs/{run['id']}/revisions", json={
        "parent_revision": generated["id"], "markdown": BODY,
    })
    assert edited.status_code == 201, edited.text
    return run, edited.json(), generated


async def enqueue_review(client, run, edit, key=None, **changes):
    return await client.post(f"/api/v1/runs/{run['id']}/revisions/{edit['id']}/reviews",
                             json={"content_hash": edit["content_hash"], **changes},
                             headers={"Idempotency-Key": key or uuid4().hex})


async def review_with_mock(app, dsn, *, wrong_hash=False, malformed=False, after_response=None):
    calls = []

    async def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        context = json.loads(body["messages"][1]["content"])
        if after_response:
            await after_response()
        review = {"approved": True, "assessed_revision": 0,
                  "assessed_hash": "0" * 64 if wrong_hash else context["content_hash"], "issues": []}
        if malformed:
            review["approved"] = "invalid"
        return httpx.Response(200, json={
            "model": "test/reviewer", "provider": "TestProvider",
            "usage": {"total_tokens": 50, "cost": "0"},
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(review)}}],
        })

    claim = await app.state.repository.claim_job("reviewer")
    assert claim is not None
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        await process_claim(app.state.repository, claim, Settings(
            _env_file=None, database_url=dsn, model_provider="openrouter", openrouter_api_key="test-key",
            openrouter_qualified=True, enable_external_calls=True, max_job_attempts=3,
            provider_rpm=10, provider_tpm=100_000,
        ), ProviderRuntime(model=OpenRouterGateway(transport, "test-key")))
    return calls


async def test_fixture_review_is_durable_separate_and_never_changes_saved_bytes(api_client, acceptance_database):
    client, app = api_client
    run, edit, generated = await saved_edit(client, app, acceptance_database)
    queued = await enqueue_review(client, run, edit, "review-idempotency")
    assert queued.status_code == 202
    job = queued.json()
    assert job["kind"] == "revision_review" and job["review_revision_id"] == edit["id"]
    same = await enqueue_review(client, run, edit, "review-idempotency")
    assert same.json()["id"] == job["id"]
    conflict = await enqueue_review(client, run, edit)
    assert conflict.status_code == 409 and conflict.json()["code"] == "review_pending"
    history = (await client.get("/api/v1/runs")).json()["runs"]
    assert [item["id"] for item in history] == [run["id"]]
    claim = await app.state.repository.claim_job("fixture-reviewer")
    assert claim is not None and claim.run_id == job["id"]
    await process_claim(app.state.repository, claim, Settings(_env_file=None, database_url=acceptance_database))
    article = (await client.get(f"/api/v1/runs/{run['id']}/article")).json()
    assert article["id"] == edit["id"] and article["content_hash"] == edit["content_hash"]
    assert article["review_status"] == "unassessed"
    assert article["assessment"]["review_status"] == "needs_review"
    assert article["assessment"]["content_hash"] == edit["content_hash"]
    assert article["assessment_job"]["state"] == "needs_review"
    assert any(issue["code"] == "synthetic_fixture" for issue in article["assessment"]["review"]["issues"])
    async with app.state.repository.pool.connection() as connection:
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            await connection.execute(
                "UPDATE revision_assessments SET review_status='approved' WHERE review_run_id=%s", (job["id"],),
            )
    for version in (edit, generated):
        downloaded = await client.get(f"/api/v1/runs/{run['id']}/revisions/{version['id']}/download")
        assert downloaded.text == version["markdown"]
    events = await client.get(f"/api/v1/runs/{job['id']}/events")
    assert "event: completed" in events.text
    cursor = [line for line in events.text.splitlines() if line.startswith("id:")][-1].split(":", 1)[1].strip()
    assert (await client.get(f"/api/v1/runs/{job['id']}/events", headers={"Last-Event-ID": cursor})).status_code == 204


@pytest.mark.parametrize("language", ["en", "tr"])
async def test_exact_hash_review_never_transfers_to_a_newer_edit(api_client, acceptance_database, language):
    client, app = api_client
    run, edit, _ = await saved_edit(client, app, acceptance_database, language=language)
    queued = await enqueue_review(client, run, edit)
    assert queued.status_code == 202
    newer = await client.post(f"/api/v1/runs/{run['id']}/revisions", json={
        "parent_revision": edit["id"], "markdown": BODY + "\n\nA new ending.",
    })
    assert newer.status_code == 201
    calls = await review_with_mock(app, acceptance_database)
    assert len(calls) == 1
    assert ("Turkish" if language == "tr" else "English") in calls[0]["messages"][0]["content"]
    current = (await client.get(f"/api/v1/runs/{run['id']}/article")).json()
    old = (await client.get(f"/api/v1/runs/{run['id']}/article", params={"revision": edit["id"]})).json()
    assert current["id"] == newer.json()["id"] and current["assessment"] is None
    assert current["review_status"] == "unassessed"
    assert old["assessment"]["revision_id"] == edit["id"]
    assert old["assessment"]["review_status"] == ("approved" if language == "en" else "needs_review")
    assert old["assessment"]["review"]["reviewer"] == "test/reviewer"


async def test_wrong_hash_is_never_published_or_retried(api_client, acceptance_database):
    client, app = api_client
    run, edit, _ = await saved_edit(client, app, acceptance_database)
    queued = (await enqueue_review(client, run, edit)).json()
    assert len(await review_with_mock(app, acceptance_database, wrong_hash=True)) == 1
    job = (await client.get(f"/api/v1/runs/{queued['id']}")).json()
    assert job["state"] == "failed" and job["error_code"] == "review_revision_mismatch"
    article = (await client.get(f"/api/v1/runs/{run['id']}/article")).json()
    assert article["assessment"] is None and article["review_status"] == "unassessed"


@pytest.mark.parametrize("delete_parent", [False, True])
async def test_late_model_response_keeps_accounting_but_cannot_publish_after_cancel(
    api_client, acceptance_database, delete_parent,
):
    client, app = api_client
    run, edit, _ = await saved_edit(client, app, acceptance_database)
    job = (await enqueue_review(client, run, edit)).json()

    async def cancel():
        if delete_parent:
            await app.state.repository.delete_run(app.state.settings.local_owner_id, run["id"])
        else:
            await app.state.repository.cancel_run(app.state.settings.local_owner_id, job["id"])

    assert len(await review_with_mock(app, acceptance_database, after_response=cancel)) == 1
    async with app.state.repository.pool.connection() as connection:
        result = await (await connection.execute(
            "SELECT r.state,(SELECT count(*) FROM revision_assessments WHERE review_run_id=r.id) AS assessments,"
            "(SELECT actual_usd FROM budget_reservations WHERE run_id=r.id LIMIT 1) AS cost "
            "FROM runs r WHERE r.id=%s", (job["id"],),
        )).fetchone()
    assert result["state"] == "cancelled" and result["assessments"] == 0 and result["cost"] == 0


async def test_review_request_enforces_owner_hash_latest_revision_and_shared_admission(api_client, acceptance_database):
    client, app = api_client
    run, edit, generated = await saved_edit(client, app, acceptance_database)
    assert (await enqueue_review(client, run, {**edit, "content_hash": "0" * 64})).status_code == 409
    assert (await enqueue_review(client, run, generated)).status_code == 409
    assert (await enqueue_review(client, run, edit, owner="forged")).status_code == 422
    cookies = dict(client.cookies)
    csrf = client.headers["X-CSRF-Token"]
    owner = app.state.settings.local_owner_id
    app.state.settings.local_owner_id = "other-review-user"
    response = await client.post("/api/v1/session")
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    assert (await enqueue_review(client, run, edit)).status_code == 404
    app.state.settings.local_owner_id = owner
    client.cookies.clear()
    client.cookies.update(cookies)
    client.headers["X-CSRF-Token"] = csrf
    app.state.repository.max_user_runs = 1
    assert (await enqueue_review(client, run, edit)).status_code == 202
    new_run = await client.post("/api/v1/runs", json={
        "topic": "Write a short story about a dragon", "research_mode": "off", "target_words": 500,
    }, headers={"Idempotency-Key": uuid4().hex})
    assert new_run.status_code == 429


async def test_reclaimed_ambiguous_dispatch_is_not_sent_again(api_client, acceptance_database):
    client, app = api_client
    run, edit, _ = await saved_edit(client, app, acceptance_database)
    job = (await enqueue_review(client, run, edit)).json()
    claim = await app.state.repository.claim_job("lost-reviewer")
    assert claim is not None
    await BudgetService(app.state.repository).reserve_call(
        claim, "ambiguous-review", max_tokens=100, estimated_cost_usd=Decimal("0"), fixture=True,
    )
    async with app.state.repository.pool.connection() as connection:
        await connection.execute("UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE run_id=%s", (job["id"],))
    await claim.close()
    calls = await review_with_mock(app, acceptance_database)
    assert not calls
    status = (await client.get(f"/api/v1/runs/{job['id']}")).json()
    assert status["state"] == "failed" and status["error_code"] == "review_interrupted"


async def test_concurrent_enqueue_and_retention_preserve_active_review_subject(api_client, acceptance_database):
    client, app = api_client
    run, edit, _ = await saved_edit(client, app, acceptance_database)
    requests = await asyncio.gather(*(enqueue_review(client, run, edit) for _ in range(3)))
    assert sorted(response.status_code for response in requests) == [202, 409, 409]
    async with app.state.repository.pool.connection() as connection:
        await connection.execute("UPDATE runs SET updated_at=clock_timestamp()-interval '60 days' WHERE id=%s", (run["id"],))
    result = await app.state.repository.purge_expired(
        event_retention_days=7, source_retention_days=7, run_retention_days=30,
        deleted_grace_hours=24,
    )
    assert result["retained_runs"] == 0
    assert (await client.get(f"/api/v1/runs/{run['id']}/article")).status_code == 200


async def test_new_source_dependent_edit_cannot_be_approved_without_research(api_client, acceptance_database):
    client, app = api_client
    run, edit, _ = await saved_edit(client, app, acceptance_database)
    newer = await client.post(f"/api/v1/runs/{run['id']}/revisions", json={
        "parent_revision": edit["id"], "markdown": BODY + "\n\nIn 2026, sales increased by 92 percent.",
    })
    assert newer.status_code == 201
    assert (await enqueue_review(client, run, newer.json())).status_code == 202
    assert len(await review_with_mock(app, acceptance_database)) == 1
    article = (await client.get(f"/api/v1/runs/{run['id']}/article")).json()
    assert article["assessment"]["review_status"] == "needs_review"
    assert any(item["code"] == "research_required" for item in article["assessment"]["review"]["issues"])


async def test_expired_evidence_is_not_replaced_by_a_new_research_call(api_client, acceptance_database):
    client, app = api_client
    run, edit, _ = await saved_edit(client, app, acceptance_database, research_mode="required")
    async with app.state.repository.pool.connection() as connection:
        await connection.execute("DELETE FROM sources WHERE run_id=%s", (run["id"],))
    assert (await enqueue_review(client, run, edit)).status_code == 202
    assert len(await review_with_mock(app, acceptance_database)) == 1
    article = (await client.get(f"/api/v1/runs/{run['id']}/article")).json()
    assert article["assessment"]["review_status"] == "needs_review"
    assert any(item["code"] == "insufficient_evidence" for item in article["assessment"]["review"]["issues"])


async def test_schema_repairs_stay_within_two_calls_and_never_create_an_assessment(api_client, acceptance_database):
    client, app = api_client
    run, edit, _ = await saved_edit(client, app, acceptance_database)
    job = (await enqueue_review(client, run, edit)).json()
    assert len(await review_with_mock(app, acceptance_database, malformed=True)) == 2
    status = (await client.get(f"/api/v1/runs/{job['id']}")).json()
    assert status["state"] == "failed" and status["model_call_count"] == 2
    assert (await client.get(f"/api/v1/runs/{run['id']}/article")).json()["assessment"] is None


async def test_reassessed_claim_counts_are_bound_to_the_exact_revision(api_client, acceptance_database):
    client, app = api_client
    run, edit, _ = await saved_edit(client, app, acceptance_database, research_mode="required")
    source = (await client.get(f"/api/v1/runs/{run['id']}/sources")).json()["sources"][0]
    updated = await client.post(f"/api/v1/runs/{run['id']}/revisions", json={
        "parent_revision": edit["id"],
        "markdown": BODY + f"\n\nThese clocks were made in 2030 [@{source['id']}].",
    })
    assert updated.status_code == 201
    target = updated.json()
    assert (await enqueue_review(client, run, target)).status_code == 202
    assert len(await review_with_mock(app, acceptance_database)) == 1
    reviewed = (await client.get(f"/api/v1/runs/{run['id']}/article")).json()
    assert reviewed["claims"][0]["assessment"] == "unsupported"
    assert reviewed["claims"][0]["article_revision_id"] == target["id"]
    assert reviewed["claims"][0]["article_hash"] == target["content_hash"]
    assert (await client.get(f"/api/v1/runs/{run['id']}")).json()["progress"]["unsupported_claims"] == 1
    newer = await client.post(f"/api/v1/runs/{run['id']}/revisions", json={
        "parent_revision": target["id"], "markdown": BODY,
    })
    assert newer.status_code == 201
    assert (await client.get(f"/api/v1/runs/{run['id']}")).json()["progress"]["unsupported_claims"] == 0
    old = (await client.get(f"/api/v1/runs/{run['id']}/article", params={"revision": target["id"]})).json()
    assert old["claims"][0]["assessment"] == "unsupported"


async def test_revision_review_migration_preserves_existing_articles_and_run_state(tmp_path):
    migrations = Path(__file__).resolve().parents[2] / "migrations"
    for path in sorted(migrations.glob("*.sql"))[:3]:
        (tmp_path / path.name).write_bytes(path.read_bytes())
    async with scoped_database(setup_saver=False, migrations_directory=tmp_path) as dsn:
        run_id, revision_id = uuid4(), uuid4()
        from fieldnote.domain import markdown_hash
        digest = markdown_hash(BODY)
        async with database_pool(dsn, migrations_directory=tmp_path) as pool:
            async with pool.connection() as connection:
                await connection.execute("INSERT INTO users(id) VALUES ('upgrade-owner')")
                await connection.execute(
                    "INSERT INTO runs(id,owner_id,request,request_hash,state,graph_version,deadline_at) "
                    "VALUES (%s,'upgrade-owner',%s,%s,'needs_review','v1',clock_timestamp())",
                    (run_id, Jsonb({"topic": "An old draft"}), "a" * 64),
                )
                await connection.execute(
                    "INSERT INTO article_revisions(id,run_id,markdown,content_hash,review_status,author_type) "
                    "VALUES (%s,%s,%s,%s,'unassessed','human')", (revision_id, run_id, BODY, digest),
                )
                await connection.execute("UPDATE runs SET current_revision=%s WHERE id=%s", (revision_id, run_id))
        expected = [path.name for path in sorted(migrations.glob("*.sql")) if path.name >= "0004"]
        assert expected[0] == "0004_revision_reviews.sql"
        assert await migrate(dsn, migrations, setup_saver=False) == expected
        assert await migrate(dsn, migrations, setup_saver=False) == []
        async with database_pool(dsn, migrations_directory=migrations) as pool:
            repository = Repository(pool, dsn)
            article = await repository.get_article("upgrade-owner", str(run_id))
            run = await repository.get_run("upgrade-owner", str(run_id))
            assert run["kind"] == "generation" and run["state"] == "needs_review"
            assert run["current_revision"] == str(revision_id)
            assert article["markdown"] == BODY and article["content_hash"] == digest
            assert article["assessment"] is None and article["review_status"] == "unassessed"
