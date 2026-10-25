"""Shared admission/accounting checks against disposable real PostgreSQL."""

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from psycopg.conninfo import make_conninfo

from fieldnote.providers.base import ProviderError
from fieldnote.services.budgets import BudgetError, BudgetService, retry_decision
from fieldnote.storage import Repository, StorageError
from fieldnote.storage.migrate import migrate
from fieldnote.storage.pool import database_pool

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


@pytest_asyncio.fixture
async def budget_database():
    base_url = os.environ.get("TEST_DATABASE_URL")
    if not base_url:
        pytest.skip("TEST_DATABASE_URL absent; real shared accounting remains unverified")
    schema = "aw_budget_" + uuid4().hex
    admin = await psycopg.AsyncConnection.connect(base_url, autocommit=True)
    await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped_url = make_conninfo(base_url, options=f"-c search_path={schema},public")
    try:
        await migrate(scoped_url, MIGRATIONS, setup_saver=False)
        async with database_pool(scoped_url, max_size=10, migrations_directory=MIGRATIONS) as pool:
            yield Repository(pool, scoped_url), scoped_url
    finally:
        await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        await admin.close()


async def claim_run(repository, owner="budget-user"):
    run = await repository.create_run(owner, {"topic": "A creative story"}, uuid4().hex)
    claim = await repository.claim_job("worker-" + uuid4().hex)
    assert claim is not None and claim.run_id == run["id"]
    return claim


@pytest.mark.asyncio
async def test_concurrent_spend_reservations_and_identity_are_atomic(budget_database):
    repository, _ = budget_database
    claim = await claim_run(repository)
    service = BudgetService(
        repository, run_budget_usd=Decimal("1"), user_daily_budget_usd=Decimal("1")
    )
    try:
        results = await asyncio.gather(
            *(
                service.reserve_call(
                    claim, f"call-{index}", max_tokens=100, estimated_cost_usd=Decimal("0.4")
                )
                for index in range(4)
            ),
            return_exceptions=True,
        )
        accepted = [result for result in results if not isinstance(result, Exception)]
        denied = [result for result in results if isinstance(result, BudgetError)]
        assert len(accepted) == 2 and len(denied) == 2
        assert all(error.code == "run_budget_exhausted" for error in denied)
        same = await service.reserve_call(
            claim, accepted[0].call_identity, max_tokens=100, estimated_cost_usd=Decimal("0.4")
        )
        assert not same.dispatch_allowed
        with pytest.raises(BudgetError, match="call_identity_conflict"):
            await service.reserve_call(
                claim, accepted[0].call_identity, max_tokens=101, estimated_cost_usd=Decimal("0.4")
            )
        run = await repository.get_run("budget-user", claim.run_id)
        assert run["model_call_count"] == 2
    finally:
        await claim.close()


@pytest.mark.asyncio
async def test_unknown_cost_stays_reserved_until_known_and_accounting_is_idempotent(
    budget_database,
):
    repository, _ = budget_database
    claim = await claim_run(repository)
    service = BudgetService(
        repository, run_budget_usd=Decimal("0.5"), user_daily_budget_usd=Decimal("1")
    )
    try:
        reservation = await service.reserve_call(
            claim, "attempt-1", max_tokens=100, estimated_cost_usd=Decimal("0.4")
        )
        unknown = await service.reconcile(reservation, outcome="unknown", retryable=True)
        assert unknown.reservation_status == "unknown" and not unknown.cost_known
        with pytest.raises(BudgetError, match="run_budget_exhausted"):
            await service.reserve_call(
                claim, "attempt-2", max_tokens=100, estimated_cost_usd=Decimal("0.2")
            )
        known = await service.reconcile(
            reservation, outcome="succeeded", actual_cost_usd=Decimal("0.1"), usage_tokens=50
        )
        assert known.cost_known and known.tokens_known and not known.reservation_overrun
        assert (
            await service.reconcile(
                reservation, outcome="succeeded", actual_cost_usd=Decimal("0.1"), usage_tokens=50
            )
            == known
        )
        with pytest.raises(BudgetError, match="accounting_conflict"):
            await service.reconcile(
                reservation, outcome="succeeded", actual_cost_usd=Decimal("0.2")
            )
        second = await service.reserve_call(
            claim, "attempt-2", max_tokens=100, estimated_cost_usd=Decimal("0.2")
        )
        assert second.dispatch_allowed
    finally:
        await claim.close()


@pytest.mark.asyncio
async def test_accounting_survives_cancel_but_new_dispatch_is_rejected(budget_database):
    repository, _ = budget_database
    claim = await claim_run(repository)
    service = BudgetService(repository)
    try:
        with pytest.raises(BudgetError, match="provider_price_unknown"):
            await service.reserve_call(claim, "unknown", max_tokens=100, estimated_cost_usd=None)
        reservation = await service.reserve_call(
            claim, "before-cancel", max_tokens=100, estimated_cost_usd=Decimal("0")
        )
        await repository.cancel_run("budget-user", claim.run_id)
        with pytest.raises(StorageError, match="no longer"):
            await service.reserve_call(
                claim, "after-cancel", max_tokens=100, estimated_cost_usd=Decimal("0")
            )
        result = await service.reconcile(
            reservation,
            outcome="succeeded",
            usage_tokens=25,
            actual_cost_usd=Decimal("0"),
            metadata={"request_id": "provider-id", "prompt": "private secret"},
        )
        assert result.cost_known and result.tokens_known
        async with repository.pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT metadata FROM provider_calls WHERE run_id=%s AND call_id=%s",
                    (UUID(claim.run_id), "before-cancel"),
                )
            ).fetchone()
        assert row["metadata"]["request_id"] == "provider-id" and "prompt" not in row["metadata"]
    finally:
        await claim.close()


@pytest.mark.asyncio
async def test_fixtures_count_call_ceiling_without_real_provider_quota(budget_database):
    repository, _ = budget_database
    claim = await claim_run(repository)
    service = BudgetService(repository, max_run_calls=2, provider_rpm=1, provider_tpm=1)
    try:
        for identity in ("fixture-1", "fixture-2"):
            assert (
                await service.reserve_call(
                    claim, identity, max_tokens=100, estimated_cost_usd=None, fixture=True
                )
            ).dispatch_allowed
        with pytest.raises(BudgetError, match="run_call_limit_exhausted"):
            await service.reserve_call(
                claim, "fixture-3", max_tokens=100, estimated_cost_usd=None, fixture=True
            )
    finally:
        await claim.close()


@pytest.mark.asyncio
async def test_daily_call_and_shared_token_limits(budget_database):
    repository, _ = budget_database
    first = await claim_run(repository)
    second = await claim_run(repository)
    service = BudgetService(repository, user_daily_calls=2, provider_tpm=150)
    try:
        one = await service.reserve_call(
            first, "first", max_tokens=100, estimated_cost_usd=Decimal("0")
        )
        with pytest.raises(BudgetError, match="provider_quota_wait"):
            await service.reserve_call(
                second, "second", max_tokens=100, estimated_cost_usd=Decimal("0")
            )
        await service.reconcile(
            one, outcome="succeeded", actual_cost_usd=Decimal("0"), usage_tokens=10
        )
        await service.reserve_call(
            second, "second", max_tokens=100, estimated_cost_usd=Decimal("0")
        )
        with pytest.raises(BudgetError, match="user_daily_call_limit_exhausted"):
            await service.reserve_call(
                first, "third", max_tokens=1, estimated_cost_usd=Decimal("0")
            )
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_daily_spend_is_shared_across_two_runs_and_unknown_price_is_not_free(budget_database):
    repository, _ = budget_database
    first = await claim_run(repository)
    second = await claim_run(repository)
    service = BudgetService(
        repository, run_budget_usd=Decimal("1"), user_daily_budget_usd=Decimal("0.5")
    )
    try:
        await service.reserve_call(first, "one", max_tokens=10, estimated_cost_usd=Decimal("0.4"))
        with pytest.raises(BudgetError, match="user_daily_budget_exhausted"):
            await service.reserve_call(
                second, "two", max_tokens=10, estimated_cost_usd=Decimal("0.2")
            )
        with pytest.raises(BudgetError, match="provider_price_unknown"):
            await service.reserve_call(second, "unknown", max_tokens=10, estimated_cost_usd=None)
        with pytest.raises(BudgetError, match="provider_token_limit_exceeded"):
            await service.reserve_call(
                second, "too-large", max_tokens=100001, estimated_cost_usd=Decimal("0")
            )
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_late_accounting_can_reconcile_after_execution_connection_is_lost(budget_database):
    repository, _ = budget_database
    claim = await claim_run(repository)
    service = BudgetService(repository)
    reservation = await service.reserve_call(
        claim, "one", max_tokens=100, estimated_cost_usd=Decimal("0")
    )
    await claim.close()
    with pytest.raises(StorageError, match="ownership"):
        await service.reserve_call(claim, "two", max_tokens=100, estimated_cost_usd=Decimal("0"))
    result = await service.reconcile(
        reservation, outcome="succeeded", actual_cost_usd=Decimal("0"), usage_tokens=40
    )
    assert result.cost_known and result.tokens_known
    repeated = await service.reconcile(reservation, outcome="unknown")
    assert repeated == result


@pytest.mark.asyncio
async def test_circuit_breaker_shared_and_repeated_accounting_does_not_multiply(budget_database):
    repository, _ = budget_database
    claim = await claim_run(repository)
    service = BudgetService(repository, circuit_failures=2)
    try:
        first = await service.reserve_call(
            claim, "first", max_tokens=10, estimated_cost_usd=Decimal("0")
        )
        await service.reconcile(first, outcome="unknown", retryable=True)
        await service.reconcile(first, outcome="unknown", retryable=True)
        second = await service.reserve_call(
            claim, "second", max_tokens=10, estimated_cost_usd=Decimal("0")
        )
        await service.reconcile(
            second, outcome="failed", retryable=True, actual_cost_usd=Decimal("0"), usage_tokens=0
        )
        with pytest.raises(BudgetError, match="provider_circuit_open") as error:
            await BudgetService(repository).reserve_call(
                claim, "third", max_tokens=10, estimated_cost_usd=Decimal("0")
            )
        assert error.value.retryable and error.value.retry_after > 0
    finally:
        await claim.close()


@pytest.mark.asyncio
async def test_actual_cost_overrun_is_recorded_before_future_admission_denial(budget_database):
    repository, _ = budget_database
    claim = await claim_run(repository)
    service = BudgetService(
        repository, run_budget_usd=Decimal("0.5"), user_daily_budget_usd=Decimal("0.5")
    )
    try:
        reservation = await service.reserve_call(
            claim, "one", max_tokens=100, estimated_cost_usd=Decimal("0.1")
        )
        result = await service.reconcile(
            reservation, outcome="succeeded", actual_cost_usd=Decimal("0.6"), usage_tokens=20
        )
        assert result.reservation_overrun
        with pytest.raises(BudgetError, match="run_budget_exhausted"):
            await service.reserve_call(claim, "two", max_tokens=1, estimated_cost_usd=Decimal("0"))
    finally:
        await claim.close()


@pytest.mark.asyncio
async def test_two_os_processes_share_one_request_window(budget_database):
    _, url = budget_database
    child = """
import asyncio,json,os,sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from fieldnote.storage import Repository
from fieldnote.storage.pool import database_pool
from fieldnote.services.budgets import BudgetService,BudgetError
async def main():
 async with database_pool(os.environ['BUDGET_TEST_URL'],migrations_directory=Path('migrations')) as pool:
  repo=Repository(pool,os.environ['BUDGET_TEST_URL'])
  await repo.create_run('budget-child-'+uuid4().hex,{'topic':'Creative story'},uuid4().hex)
  claim=await repo.claim_job('worker-'+uuid4().hex)
  try:
   service=BudgetService(repo,provider_scope='subprocess-test',provider_rpm=1)
   try:
    await service.reserve_call(claim,'one',max_tokens=10,estimated_cost_usd=Decimal('0'))
    print(json.dumps({'accepted':True}))
   except BudgetError as error:print(json.dumps({'accepted':False,'code':error.code}))
  finally:await claim.close()
asyncio.run(main(),loop_factory=asyncio.SelectorEventLoop)
"""
    environment = dict(os.environ, BUDGET_TEST_URL=url, PYTHONPATH="src")

    async def invoke():
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", child],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    results = await asyncio.gather(invoke(), invoke())
    assert sum(row["accepted"] for row in results) == 1
    assert next(row for row in results if not row["accepted"])["code"] == "provider_quota_wait"


def test_retry_policy_has_one_bounded_owner_and_respects_provider_wait():
    now = datetime.now(UTC)
    decision = retry_decision(
        ProviderError("rate", retryable=True, retry_after=20),
        attempt=1,
        deadline=now + timedelta(seconds=30),
        now=now,
        remaining_calls=3,
    )
    assert decision.retry and decision.delay_seconds == 20
    assert not retry_decision(
        ProviderError("auth"), attempt=1, deadline=now + timedelta(seconds=30), remaining_calls=3
    ).retry
    assert not retry_decision(
        ProviderError("rate", retryable=True),
        attempt=3,
        deadline=now + timedelta(seconds=30),
        remaining_calls=3,
    ).retry
    assert not retry_decision(
        ProviderError("rate", retryable=True, retry_after=40),
        attempt=1,
        deadline=now + timedelta(seconds=30),
        now=now,
        remaining_calls=3,
    ).retry
