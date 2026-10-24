# AgentWriter

Source-aware article drafting with durable runs, transparent evidence and human review.

AgentWriter turns a brief into a structured Markdown draft, shows the evidence and
review findings behind it, and keeps every revision. The default local profile uses
fixture model and research providers, so it is safe to explore without credentials
or network calls. A draft is never presented as approved or fact checked by the
fixture.

## Start here

- [User guide](docs/user-guide.md) — create, review, edit, download, reopen, cancel and delete drafts.
- [Setup and operations](docs/setup-and-operations.md) — Windows/Linux setup, PostgreSQL, API/worker, probes, tests, evaluation and Compose.
- [Configuration](docs/configuration.md) — settings, provider gates and operational bounds.
- [Workflow](docs/workflow.md) — how the writing graph, review and citations work.

## Local quick start

```powershell
uv venv .venv --python python
uv pip sync --python .venv\Scripts\python.exe --require-hashes --only-binary :all: --index-url https://pypi.org/simple requirements-dev.lock
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
Copy-Item .env.example .env
# Set DATABASE_URL and MIGRATION_DATABASE_URL in .env, then:
.\.venv\Scripts\python.exe -m agentwriter.storage.migrate --directory migrations --runtime-role agentwriter_runtime
.\.venv\Scripts\python.exe -m agentwriter --env-file .env serve
# In a second terminal:
.\.venv\Scripts\python.exe -m agentwriter --env-file .env worker
```

Open `http://127.0.0.1:8000`. The full command sequence, Linux instructions and
Docker Compose profile are in [Setup and operations](docs/setup-and-operations.md).

## Repository map

| Path | Purpose |
|---|---|
| `src/agentwriter/` | FastAPI app, browser client, LangGraph workflow, providers, storage and worker. |
| `migrations/` | Checksummed PostgreSQL schema migrations. |
| `tests/` | Unit and real-PostgreSQL integration tests. |
| `scripts/` | Fresh-checkout smoke test, corpus generator and deterministic evaluator. |
| `evaluation/corpus/v1/` | Versioned quality corpus, editorial rubric and regression baseline. |
| `ops/` | Prometheus alert rules and retention service/timer units. |
| `docs/` | User, setup, configuration, workflow, provider and runbook documentation. |
| `Dockerfile`, `compose.yaml` | Non-root production-shaped local image and loopback Compose profile. |
| `requirements.in`, `requirements.lock` | Direct runtime pins and hashed transitive runtime lock. |
| `requirements-dev.in`, `requirements-dev.lock` | Direct development pins and hashed transitive development lock. |

## What is verified locally

- Durable create → research → draft → review → edit → download → reopen, with a
  real PostgreSQL database and a separately supervised worker.
- Human edits are stored as new, `unassessed` revisions; approval never carries
  over. Concurrent edits return a recoverable conflict.
- Two local owners on one database are isolated: one owner cannot read or delete
  another owner's run.
- Citation identity, research-off boundaries, cancellation, deletion tombstones,
  retention, encrypted backup/restore, alert inputs and the Compose lifecycle.
- 38 snapshot evaluation cases (32 valid briefs and 6 expected rejections) pass
  through the production graph assembly path; the result is pinned in the
  committed baseline.

## Boundaries

This repository is not a qualified production release. The following remain
unverified or blocked until real accounts, infrastructure or human reviewers are
available:

- live OpenRouter and Treg provider qualification;
- hosted OIDC identity (the API fails closed for `AUTH_MODE=oidc`);
- human factual/editorial review and judge agreement;
- deployed load, fault-injection and release acceptance;
- a manual screen-reader pass before a public release.

The local profile binds to loopback, uses local single-owner authentication and
makes no live provider calls. Do not expose it as a public service. Keep `.env`,
credentials, backups and `.local/` artifacts out of Git.
