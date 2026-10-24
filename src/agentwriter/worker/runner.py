"""Run one claimed graph on the exact PostgreSQL session owning its advisory lock."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from dataclasses import replace
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from agentwriter.domain import CreateRunRequest
from agentwriter.graph import (
    GraphError,
    GraphHooks,
    build_graph,
    initial_state,
    make_fixture_dependencies,
)
from agentwriter.providers.base import ProviderError
from agentwriter.settings import Settings
from agentwriter.storage.pool import database_pool
from agentwriter.storage.repository import Claim, Repository, StorageError

logger = logging.getLogger("agentwriter.worker")


class AccountedGateway:
    """Central reservation/accounting wrapper; the transport never retries itself."""

    def __init__(self, gateway, budget, claim):
        self.gateway, self.budget, self.claim = gateway, budget, claim

    async def generate(self, schema, messages, call_identity, max_tokens=2048, **kwargs):
        from decimal import Decimal

        reservation = await self.budget.reserve_call(
            self.claim, f"attempt:{self.claim.generation}:{call_identity}", max_tokens=max_tokens,
            estimated_cost_usd=Decimal("0"), fixture=True,
        )
        if not reservation.dispatch_allowed:
            raise ProviderError("call_already_reserved", outcome_unknown=True)
        try:
            result = await self.gateway.generate(schema, messages, call_identity, max_tokens, **kwargs)
        except BaseException:
            await self.budget.reconcile(reservation, outcome="unknown")
            raise
        await self.budget.reconcile(reservation, usage_tokens=0, actual_cost_usd=Decimal("0"),
                                    outcome="succeeded", metadata={"model": result.resolved_model})
        return result


async def execute_claim(repository: Repository, claim: Claim, settings: Settings) -> dict:
    """Only synthetic fixture execution is enabled until external qualification gates pass."""
    from agentwriter.services.budgets import BudgetService

    if settings.model_provider != "fixture" or settings.research_provider != "fixture":
        raise ProviderError("external_execution_unqualified")
    request = CreateRunRequest.model_validate(claim.run["request"])

    async def on_plan(plan):
        await repository.save_plan(claim, plan)

    async def on_source(source, excerpt):
        await repository.save_sources(claim, [source], [excerpt])

    async def on_section(section):
        await repository.save_section(claim, section["task_id"], section["revision"], section)

    async def check_active():
        await repository.check_active(claim)

    hooks = GraphHooks(on_plan=on_plan, on_source=on_source, on_section=on_section,
                       check_active=check_active)
    deps = make_fixture_dependencies(request, hooks=hooks, max_review_rounds=settings.max_review_rounds,
                                     max_model_calls=settings.max_model_calls)
    budget = BudgetService(repository, max_run_calls=settings.max_model_calls,
                           provider_rpm=settings.provider_rpm, provider_tpm=settings.provider_tpm,
                           run_budget_usd=settings.run_budget_usd,
                           user_daily_budget_usd=settings.user_daily_budget_usd)
    deps = replace(deps, model=AccountedGateway(deps.model, budget, claim))
    saver = AsyncPostgresSaver(
        claim.connection,
        serde=JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=None,
                                 allowed_json_modules=None),
    )
    graph = build_graph(deps).compile(checkpointer=saver)
    config: RunnableConfig = {
        "configurable": {"thread_id": claim.run_id},
        "recursion_limit": 80,
    }
    snapshot = await graph.aget_state(config)
    seed = None if snapshot.values else initial_state(claim.run_id)
    output = await graph.ainvoke(seed, config=config, durability="sync")
    # A completed graph may be recovered after a crash before finalization. Read
    # its saved artifact rather than regenerate, then commit state/event atomically.
    raw = output["assembled_markdown"]
    if output.get("fixture_calls", 0):
        raw = "> Synthetic development draft. No live model or web research was used.\n\n" + raw
    elif not output["decision"]["need_research"]:
        raw = "> Unresearched draft. Review factual claims before publication.\n\n" + raw
    await repository.finalize(
        claim, output["final_markdown"], review_status=output["review_status"], issues=output["issues"],
        citation_map={"sources": output.get("sources", {}), "editable_markdown": raw},
    )
    return output


async def process_claim(repository: Repository, claim: Claim, settings: Settings) -> None:
    task = asyncio.create_task(execute_claim(repository, claim, settings))

    async def heartbeat():
        while True:
            await asyncio.sleep(settings.job_heartbeat_seconds)
            if not await repository.heartbeat(claim):
                task.cancel()
                return

    keeper = asyncio.create_task(heartbeat())
    try:
        await task
    except asyncio.CancelledError:
        with contextlib.suppress(StorageError):
            await repository.fail_job(claim, "execution_cancelled", "Execution stopped. Remaining remote costs may be unknown.")
        raise
    except (GraphError, ProviderError, StorageError) as exc:
        code = getattr(exc, "code", "generation_failed")
        with contextlib.suppress(StorageError):
            await repository.fail_job(claim, code, "This draft could not finish. Review the run or start again.")
    except Exception as exc:
        logger.error("run_failed run_id=%s exception_type=%s", claim.run_id, type(exc).__name__)
        with contextlib.suppress(StorageError):
            await repository.fail_job(claim, "generation_failed", "This draft could not finish. Try again later.")
    finally:
        keeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keeper
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await claim.close()


async def serve_worker(settings: Settings, *, once: bool = False) -> None:
    if settings.database_url is None:
        raise RuntimeError("Set DATABASE_URL and run migrations before starting the worker.")
    if settings.model_provider != "fixture" or settings.research_provider != "fixture":
        raise RuntimeError("External worker execution awaits provider and durable-policy qualification.")
    dsn = settings.database_url.get_secret_value()
    worker_id = f"worker-{uuid4()}"
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    async with database_pool(dsn, max_size=settings.database_pool_max_size,
                             timeout=settings.database_pool_timeout) as pool:
        repository = Repository(pool, dsn, max_run_seconds=settings.max_run_seconds)
        await repository.worker_heartbeat(worker_id)

        async def registry():
            while not stop.is_set():
                await repository.worker_heartbeat(worker_id)
                await asyncio.sleep(min(settings.job_heartbeat_seconds, settings.worker_stale_seconds / 3))

        registry_task = asyncio.create_task(registry())
        try:
            while not stop.is_set():
                claim = await repository.claim_job(worker_id, lease_seconds=settings.job_lease_seconds)
                if claim:
                    # Total timeout includes the queue/retry time through the DB barrier.
                    try:
                        async with asyncio.timeout(settings.max_run_seconds):
                            await process_claim(repository, claim, settings)
                    except TimeoutError:
                        logger.warning("run_deadline run_id=%s", claim.run_id)
                if once:
                    return
                try:
                    await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_seconds)
                except TimeoutError:
                    pass
        finally:
            stop.set()
            registry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await registry_task
            await repository.worker_heartbeat(worker_id, draining=True)
