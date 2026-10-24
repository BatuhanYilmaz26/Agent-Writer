"""Worker retry, partial-artifact and supervised-entry acceptance."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agentwriter.domain import CreateRunRequest
from agentwriter.graph import make_fixture_dependencies
from agentwriter.providers.base import ModelT, ProviderError
from agentwriter.settings import Settings
from agentwriter.storage import Repository
from agentwriter.storage.pool import database_pool
from agentwriter.worker.runner import (
    ProviderRuntime,
    process_claim,
    serve_worker,
)

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
pytestmark = pytest.mark.asyncio


def brief() -> dict[str, Any]:
    return {
        "topic": "Write a short story about a dragon",
        "audience": "General readers",
        "tone": "clear",
        "language": "en",
        "target_words": 500,
        "research_mode": "required",
        "instructions": "",
    }


class FlakyGateway:
    """Delegate to fixture responses while injecting one classified failure."""

    def __init__(self, inner: Any, fail_identity: str, error: ProviderError, times: int = 1):
        self.inner = inner
        self.fail_identity = fail_identity
        self.error = error
        self.times = times

    async def generate(
        self,
        schema: type[ModelT],
        messages: list[dict[str, str]],
        call_identity: str,
        max_tokens: int = 2048,
        **kwargs: Any,
    ):
        if self.times and call_identity == self.fail_identity:
            self.times -= 1
            raise self.error
        return await self.inner.generate(schema, messages, call_identity, max_tokens, **kwargs)


class SlowGateway:
    """Hold one provider call open to exercise lease heartbeat/drain behavior."""

    def __init__(self, inner: Any, slow_identity: str, delay: float):
        self.inner = inner
        self.slow_identity = slow_identity
        self.delay = delay

    async def generate(
        self,
        schema: type[ModelT],
        messages: list[dict[str, str]],
        call_identity: str,
        max_tokens: int = 2048,
        **kwargs: Any,
    ):
        if call_identity == self.slow_identity:
            await asyncio.sleep(self.delay)
        return await self.inner.generate(schema, messages, call_identity, max_tokens, **kwargs)


async def test_worker_retries_a_transient_plan_failure_then_finishes(
    acceptance_database: str,
) -> None:
    settings = Settings(
        _env_file=None,
        database_url=acceptance_database,
        max_user_nonterminal_runs=10,
        max_job_attempts=3,
    )
    request = CreateRunRequest.model_validate(brief())
    inner = make_fixture_dependencies(request).model
    runtime = ProviderRuntime(
        model=FlakyGateway(
            inner,
            "plan",
            ProviderError("provider_rate_limited", retryable=True, retry_after=0),
        )
    )
    async with database_pool(acceptance_database, migrations_directory=MIGRATIONS) as pool:
        repository = Repository(pool, acceptance_database, max_run_seconds=600)
        run = await repository.create_run(
            settings.local_owner_id, request.model_dump(mode="json"), "retry-run"
        )
        first = await repository.claim_job("retry-worker")
        assert first is not None
        await process_claim(repository, first, settings, runtime)
        waiting = await repository.get_run(settings.local_owner_id, run["id"])
        assert waiting["state"] == "retry_wait"
        assert waiting["error_code"] == "provider_rate_limited"
        await asyncio.sleep(0.7)
        assert await repository.activate_due_retries() == 1
        second = await repository.claim_job("retry-worker")
        assert second is not None and second.generation > first.generation
        await process_claim(repository, second, settings, runtime)
        finished = await repository.get_run(settings.local_owner_id, run["id"])
        assert finished["state"] == "needs_review"
        article = await repository.get_article(settings.local_owner_id, run["id"])
        assert article["review_status"] == "needs_review"
        events = await repository.list_events(settings.local_owner_id, run["id"])
        assert [event["event_type"] for event in events].count("retry_wait") == 1
        assert [event["event_type"] for event in events].count("plan_ready") == 1
        assert events[-1]["event_type"] == "completed"


async def test_worker_retains_a_partial_artifact_when_review_fails(
    acceptance_database: str,
) -> None:
    settings = Settings(
        _env_file=None,
        database_url=acceptance_database,
        max_user_nonterminal_runs=10,
    )
    request = CreateRunRequest.model_validate(brief())
    inner = make_fixture_dependencies(request).model
    runtime = ProviderRuntime(
        model=FlakyGateway(inner, "review:0", ProviderError("provider_policy_denied"))
    )
    async with database_pool(acceptance_database, migrations_directory=MIGRATIONS) as pool:
        repository = Repository(pool, acceptance_database, max_run_seconds=600)
        run = await repository.create_run(
            settings.local_owner_id, request.model_dump(mode="json"), "partial-run"
        )
        claim = await repository.claim_job("partial-worker")
        assert claim is not None
        await process_claim(repository, claim, settings, runtime)
        failed = await repository.get_run(settings.local_owner_id, run["id"])
        assert failed["state"] == "failed"
        assert failed["error_code"] == "provider_policy_denied"
        article = await repository.get_article(settings.local_owner_id, run["id"])
        assert article["review_status"] == "failed"
        assert "Synthetic development draft" in article["markdown"]
        assert "# " in article["markdown"]
        assert any(
            issue["code"] == "provider_policy_denied" for issue in article["issues"]
        )
        events = await repository.list_events(settings.local_owner_id, run["id"])
        assert events[-1]["event_type"] == "failed"


async def test_heartbeat_keeps_a_slow_provider_call_leased(
    acceptance_database: str,
) -> None:
    settings = Settings(
        _env_file=None,
        database_url=acceptance_database,
        max_user_nonterminal_runs=10,
        job_heartbeat_seconds=1,
        job_lease_seconds=3,
        max_job_attempts=1,
    )
    request = CreateRunRequest.model_validate(brief())
    inner = make_fixture_dependencies(request).model
    runtime = ProviderRuntime(model=SlowGateway(inner, "plan", 4.0))
    async with database_pool(acceptance_database, migrations_directory=MIGRATIONS) as pool:
        repository = Repository(pool, acceptance_database, max_run_seconds=600)
        run = await repository.create_run(
            settings.local_owner_id, request.model_dump(mode="json"), "slow-provider"
        )
        claim = await repository.claim_job("heartbeat-worker")
        assert claim is not None
        await process_claim(repository, claim, settings, runtime)
        finished = await repository.get_run(settings.local_owner_id, run["id"])
        assert finished["state"] == "needs_review"
        assert finished["error_code"] is None


async def test_worker_drains_an_active_claim_into_a_retry(
    acceptance_database: str,
) -> None:
    settings = Settings(
        _env_file=None,
        database_url=acceptance_database,
        max_user_nonterminal_runs=10,
        job_heartbeat_seconds=1,
        job_lease_seconds=3,
        worker_drain_seconds=2,
        scheduler_poll_seconds=0.1,
        worker_poll_seconds=0.1,
        max_job_attempts=3,
    )
    request = CreateRunRequest.model_validate(brief())
    inner = make_fixture_dependencies(request).model
    runtime = ProviderRuntime(model=SlowGateway(inner, "plan", 30.0))
    async with database_pool(acceptance_database, migrations_directory=MIGRATIONS) as pool:
        repository = Repository(pool, acceptance_database, max_run_seconds=600)
        run = await repository.create_run(
            settings.local_owner_id, request.model_dump(mode="json"), "drain-run"
        )
    stop = asyncio.Event()
    task = asyncio.create_task(serve_worker(settings, stop=stop, runtime=runtime))
    try:
        for _ in range(100):
            async with database_pool(acceptance_database, migrations_directory=MIGRATIONS) as pool:
                repository = Repository(pool, acceptance_database, max_run_seconds=600)
                current = await repository.get_run(settings.local_owner_id, run["id"])
            if current["state"] == "running":
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("worker did not claim the run")
        stop.set()
        await asyncio.wait_for(task, timeout=15)
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async with database_pool(acceptance_database, migrations_directory=MIGRATIONS) as pool:
        repository = Repository(pool, acceptance_database, max_run_seconds=600)
        drained = await repository.get_run(settings.local_owner_id, run["id"])
        assert drained["state"] == "retry_wait"
        assert drained["error_code"] == "worker_shutdown"
        workers = await repository.workers(stale_seconds=60)
        assert workers and workers[0]["draining"] is True
        assert workers[0]["active_count"] == 0
        async with pool.connection() as connection:
            job = await (
                await connection.execute(
                    "SELECT lease_owner FROM jobs WHERE run_id=%s", (run["id"],)
                )
            ).fetchone()
        assert job["lease_owner"] is None


async def test_supervised_worker_once_claims_and_finishes_a_fixture_run(
    acceptance_database: str,
) -> None:
    settings = Settings(
        _env_file=None,
        database_url=acceptance_database,
        max_user_nonterminal_runs=10,
        scheduler_poll_seconds=0.1,
        worker_poll_seconds=0.1,
    )
    async with database_pool(acceptance_database, migrations_directory=MIGRATIONS) as pool:
        repository = Repository(pool, acceptance_database, max_run_seconds=600)
        run = await repository.create_run(
            settings.local_owner_id, brief(), "supervised-run"
        )
    await serve_worker(settings, once=True)
    async with database_pool(acceptance_database, migrations_directory=MIGRATIONS) as pool:
        repository = Repository(pool, acceptance_database, max_run_seconds=600)
        finished = await repository.get_run(settings.local_owner_id, run["id"])
        assert finished["state"] == "needs_review"
        assert (await repository.workers(stale_seconds=60))[0]["draining"] is True
