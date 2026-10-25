"""Exercise every operational alert condition against durable PostgreSQL state."""

from __future__ import annotations

import psycopg
import pytest

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


async def test_alert_conditions_are_visible_in_metrics_and_readiness(
    api_client, acceptance_database: str
) -> None:
    client, app = api_client
    created = await client.post(
        "/api/v1/runs", json=brief(), headers={"Idempotency-Key": "alert-run"}
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    leased = await client.post(
        "/api/v1/runs", json=brief(), headers={"Idempotency-Key": "lease-run"}
    )
    assert leased.status_code == 202, leased.text
    leased_id = leased.json()["id"]

    async with await psycopg.AsyncConnection.connect(acceptance_database, autocommit=True) as db:
        # Queue backlog and deadline overrun.
        await db.execute(
            "UPDATE runs SET accepted_at=clock_timestamp()-interval '10 minutes',"
            " deadline_at=clock_timestamp()-interval '1 minute' WHERE id=%s",
            (run_id,),
        )
        # Expired worker lease on a running job.
        await db.execute(
            "UPDATE runs SET state='running', deadline_at=clock_timestamp()-interval '1 minute'"
            " WHERE id=%s",
            (leased_id,),
        )
        await db.execute(
            "UPDATE jobs SET lease_owner='dead-worker',"
            " lease_expires_at=clock_timestamp()-interval '1 minute' WHERE run_id=%s",
            (leased_id,),
        )
        # Dead worker heartbeat.
        await db.execute(
            "INSERT INTO worker_heartbeats(id,graph_version,draining,last_seen,active_count)"
            " VALUES ('dead-worker','graph-v1',false,clock_timestamp()-interval '10 minutes',1)"
        )
        # Unknown provider spend and failed provider calls.
        await db.execute(
            "INSERT INTO budget_reservations(run_id,call_id,amount_usd,actual_usd,status)"
            " VALUES (%s,'unknown-call',1.5,NULL,'unknown')",
            (run_id,),
        )
        for index in range(6):
            await db.execute(
                "INSERT INTO provider_calls(run_id,call_id,attempt,status)"
                " VALUES (%s,%s,1,'failed')",
                (run_id, f"failed-{index}"),
            )

    metrics = await client.get("/metrics")
    assert metrics.status_code == 200
    body = metrics.text
    assert 'fieldnote_workers{state="dead"} 1' in body
    assert 'fieldnote_workers{state="alive"} 0' in body
    assert "fieldnote_queue_oldest_wait_seconds 600" in body
    assert "fieldnote_runs_overdue 2" in body
    assert "fieldnote_expired_leases 1" in body
    assert 'fieldnote_provider_calls{status="failed"} 6' in body
    assert "fieldnote_spend_unknown_usd 1.50000000" in body
    assert "fieldnote_spend_unknown_calls 1" in body

    # A failing readiness dependency must produce a probe failure, not a 500.
    async def unavailable(*, freshness_seconds: int) -> bool:
        del freshness_seconds
        raise RuntimeError("database unavailable")

    app.state.repository.ready = unavailable
    readiness = await client.get("/ready")
    assert readiness.status_code == 503
    assert readiness.json() == {"status": "unavailable"}
