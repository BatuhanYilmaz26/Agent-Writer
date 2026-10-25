"""Real storage, worker and revision round trips for five article languages."""

import hashlib

import pytest

from fieldnote.domain.languages import language_profile
from fieldnote.settings import Settings
from fieldnote.storage import Repository
from fieldnote.storage.pool import database_pool
from fieldnote.worker.runner import process_claim

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("language,text", [
    ("en", "A thoughtful draft about a forest."),
    ("tr", "Çağrı, ağaçların büyümesini ve doğayı öğreniyor."),
    ("es", "El niño aprende cómo crecen los árboles cada año."),
    ("pt", "A avó observa a ação da chuva e o coração da floresta."),
    ("de", "Über große Bäume, schöne Wälder und äußere Einflüsse."),
])
async def test_language_survives_generation_reopen_edit_and_download(
    api_client, acceptance_database, language, text,
):
    client, _ = api_client
    response = await client.post(
        "/api/v1/runs",
        json={"topic": text, "language": language, "target_words": 500, "research_mode": "required"},
        headers={"Idempotency-Key": "language-" + language},
    )
    assert response.status_code == 202
    run_id = response.json()["id"]
    async with database_pool(acceptance_database) as pool:
        repository = Repository(pool, acceptance_database)
        await repository.worker_heartbeat("language-acceptance")
        claim = await repository.claim_job("language-acceptance")
        assert claim is not None
        await process_claim(repository, claim, Settings(_env_file=None))
    base = f"/api/v1/runs/{run_id}"
    reopened = (await client.get(base)).json()
    assert reopened["request"]["language"] == language
    assert reopened["state"] == "needs_review"
    article = (await client.get(base + "/article")).json()
    profile = language_profile(language)
    assert article["citation_map"]["language"] == language
    assert profile.synthetic_notice in article["markdown"]
    assert f"## {profile.references}" in article["markdown"]
    source_id = next(iter(article["citation_map"]["sources"]))
    edited = await client.post(base + "/revisions", json={
        "parent_revision": article["id"], "markdown": f"# {text}\n\n{text} [@{source_id}]",
    })
    assert edited.status_code == 201
    revision = edited.json()
    assert revision["review_status"] == "unassessed"
    exact = (await client.get(base + f"/article?revision={revision['id']}")).json()
    assert text in exact["markdown"]
    assert f"## {profile.references}" in exact["markdown"]
    assert exact["citation_map"]["language"] == language
    download = await client.get(base + f"/revisions/{revision['id']}/download")
    assert download.status_code == 200
    assert download.headers["content-language"] == language
    assert download.content.decode("utf-8") == exact["markdown"]
    assert hashlib.sha256(download.content).hexdigest() == exact["content_hash"]
    original = (await client.get(base + f"/article?revision={article['id']}")).json()
    assert original["content_hash"] == article["content_hash"]


async def test_api_rejects_an_unconfigured_language_before_creating_a_run(api_client):
    client, _ = api_client
    response = await client.post(
        "/api/v1/runs", json={"topic": "A forest", "language": "fr"},
        headers={"Idempotency-Key": "unsupported-language"},
    )
    assert response.status_code == 422


@pytest.mark.parametrize("language", ["en", "tr", "es", "pt", "de"])
async def test_short_profile_brief_round_trip_and_idempotency(api_client, acceptance_database, language):
    from tests.integration.test_api import run_one_fixture_job

    client, _ = api_client
    payload = {"topic": "A technical explanation", "language": language, "target_words": 350,
               "editorial_profile": "technical-v1", "angle": "A practical limitation",
               "author_notes": "My own observation", "destinations": ["medium", "linkedin"],
               "research_mode": "required"}
    headers = {"Idempotency-Key": "short-profile"}
    created = await client.post("/api/v1/runs", json=payload, headers=headers)
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    duplicate = await client.post("/api/v1/runs", json=payload, headers=headers)
    assert duplicate.json()["id"] == run_id
    conflict = await client.post("/api/v1/runs", json={**payload, "angle": "Another angle"}, headers=headers)
    assert conflict.status_code == 409
    await run_one_fixture_job(acceptance_database)
    reopened = (await client.get(f"/api/v1/runs/{run_id}")).json()
    for key, value in payload.items():
        assert reopened["request"][key] == value
    article = (await client.get(f"/api/v1/runs/{run_id}/article")).json()
    assert article["review_status"] == "needs_review"
    assert len(article["citation_map"]["editable_markdown"].split()) < 450


async def test_changing_language_does_not_reuse_another_language_run(api_client):
    client, _ = api_client
    headers = {"Idempotency-Key": "same-brief-different-language"}
    first = await client.post("/api/v1/runs", json={"topic": "A forest", "language": "en"}, headers=headers)
    assert first.status_code == 202
    conflict = await client.post("/api/v1/runs", json={"topic": "A forest", "language": "es"}, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"
