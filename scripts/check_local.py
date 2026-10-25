"""Exercise an already-running loopback fixture app without using live providers.

Only runs created by this check are removed. Use an isolated local profile for
capacity measurements; the report does not qualify hosted deployment or quality.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx


class CheckError(ValueError):
    pass


def loopback_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        raise CheckError("Use a valid explicit loopback HTTP(S) URL.") from None
    if (not loopback or parsed.scheme not in {"http", "https"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise CheckError("Use an explicit loopback HTTP(S) URL, such as http://127.0.0.1:8000.")
    return value.rstrip("/")


def percentile(values: list[float], fraction: float) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


async def check_local(client: httpx.AsyncClient, *, runs: int = 100) -> dict:
    if not 1 <= runs <= 1000:
        raise CheckError("Choose between 1 and 1000 runs.")
    ready = await client.get("/ready")
    if ready.status_code != 200:
        raise CheckError("The app is not ready. Start its database, API and worker first.")
    session = await client.post("/api/v1/session")
    if session.status_code != 200:
        raise CheckError("The local session could not be started.")
    profile = session.json()
    csrf = profile.get("csrf_token")
    if not isinstance(csrf, str) or not csrf:
        raise CheckError("The app did not return a valid local session.")
    client.headers["X-CSRF-Token"] = csrf
    if not (
        profile.get("mode") == "local" and profile.get("model_provider") == "fixture"
        and profile.get("research_provider") == "fixture"
        and profile.get("external_calls_enabled") is False
    ):
        await client.delete("/api/v1/session")
        raise CheckError("This check requires both fixture providers and external calls disabled.")
    enqueue_times: list[float] = []
    total_times: list[float] = []
    states: dict[str, int] = {}
    deleted = 0
    started = time.monotonic()
    try:
        for _ in range(runs):
            run_started = time.monotonic()
            response = await client.post(
                "/api/v1/runs", headers={"Idempotency-Key": "local-check-" + uuid4().hex},
                json={"topic": "A practical guide to building a writing habit",
                      "target_words": 500, "research_mode": "required"},
            )
            enqueue_times.append(time.monotonic() - run_started)
            if response.status_code != 202:
                raise CheckError(f"A fixture run was not admitted (HTTP {response.status_code}).")
            run_id = response.json()["id"]
            base = "/api/v1/runs/" + run_id
            terminal = False
            try:
                while time.monotonic() - run_started < 60:
                    status = await client.get(base)
                    if status.status_code != 200:
                        raise CheckError("The created run could not be reopened.")
                    state = status.json()["state"]
                    if state in {"succeeded", "needs_review", "failed", "cancelled"}:
                        terminal = True
                        break
                    await asyncio.sleep(0.1)
                if not terminal:
                    raise CheckError("A fixture run exceeded the 60-second check deadline.")
                if state not in {"succeeded", "needs_review"}:
                    code = status.json().get("error_code")
                    if code == "user_daily_call_limit_exhausted":
                        raise CheckError(
                            "The fixture daily call limit was reached. Use fewer runs or "
                            "a separately configured disposable benchmark profile."
                        )
                    raise CheckError(f"A fixture run failed or was cancelled after {len(total_times)} completed checks.")
                article = await client.get(base + "/article")
                if article.status_code != 200:
                    raise CheckError("The completed article is unavailable.")
                revision = article.json()
                download = await client.get(base + "/revisions/" + revision["id"] + "/download")
                digest = hashlib.sha256(download.content).hexdigest()
                if download.status_code != 200 or digest != revision["content_hash"]:
                    raise CheckError("The downloaded article does not match its saved hash.")
                if not download.text.startswith("> Synthetic development draft."):
                    raise CheckError("The fixture article is missing its synthetic disclosure.")
                replay = await client.get(base + "/events")
                if replay.status_code == 429:
                    raise CheckError("Close other progress views before running the isolated fixture check.")
                ids = [int(line[3:].strip()) for line in replay.text.splitlines() if line.startswith("id:")]
                if (replay.status_code != 200 or not ids or ids != sorted(set(ids))
                        or "event: completed" not in replay.text):
                    raise CheckError(f"Committed progress replay failed (HTTP {replay.status_code}).")
                exhausted = await client.get(base + "/events", headers={"Last-Event-ID": str(ids[-1])})
                if exhausted.status_code != 204:
                    raise CheckError("Terminal progress did not return an empty 204 response.")
                states[state] = states.get(state, 0) + 1
                total_times.append(time.monotonic() - run_started)
            finally:
                if not terminal:
                    await client.post(base + "/cancel")
                removal = await client.delete(base)
                if removal.status_code == 204:
                    deleted += 1
    finally:
        await client.delete("/api/v1/session")
    if deleted != runs:
        raise CheckError("Some check runs could not be removed; inspect local run history.")
    return {
        "checked_at_utc": datetime.now(UTC).isoformat(), "fixture_only": True,
        "runs": runs, "completed": len(total_times), "deleted": deleted, "states": states,
        "seconds": round(time.monotonic() - started, 3),
        "enqueue_p95_ms": round(percentile(enqueue_times, 0.95) * 1000, 3),
        "run_p50_ms": round(percentile(total_times, 0.5) * 1000, 3),
        "run_p95_ms": round(percentile(total_times, 0.95) * 1000, 3),
        "scope": "Serial loopback fixture workflow; not live-model quality or deployed capacity.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--runs", type=int, default=100)
    parser.add_argument("--output", type=Path, default=Path(".local/local-check.json"))
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists. Choose a new report filename.")

    async def run() -> dict:
        async with httpx.AsyncClient(
            base_url=loopback_url(args.url), trust_env=False, follow_redirects=False,
            timeout=70,
        ) as client:
            return await check_local(client, runs=args.runs)

    try:
        report = asyncio.run(run())
    except CheckError as exc:
        parser.error(str(exc))
    except httpx.HTTPError:
        parser.error("The local app could not be reached. Check its URL and running processes.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
