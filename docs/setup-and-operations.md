# Setup and operations

This guide covers a local, production-shaped installation of AgentWriter. The
default profile uses fixture model and research providers: it makes no live AI
or web request and needs no provider credentials. It is a complete workflow
demonstration, not a production release.

For the browser journey, see the [user guide](user-guide.md). For every setting
and provider gate, see [configuration](configuration.md) and
[providers](providers.md).

## What you need

- Windows 10/11 or a current Linux distribution.
- CPython 3.13 (the project requires `>=3.13,<3.14`).
- [uv](https://docs.astral.sh/uv/) for the hash-locked environment.
- PostgreSQL 18, either installed locally or started with Docker.
- Docker Engine/Desktop only if you want the Compose profile.

No OpenRouter, Treg or Tavily account is needed for the fixture profile.
Before using live models, qualify your OpenRouter account with the
[one-request check](providers.md#check-your-openrouter-account). Treg has a qualified
route, but another account or routing change needs its own check. See the
[provider guide](providers.md) before enabling external calls.

## Install the application

### Windows (PowerShell)

From the repository root:

```powershell
uv venv .venv --python python
uv pip sync --python .venv\Scripts\python.exe --require-hashes --only-binary :all: --index-url https://pypi.org/simple requirements-dev.lock
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
.\.venv\Scripts\python.exe -m agentwriter --help
```

`uv venv` does not install `pip`; use `uv pip` for this environment. If you
create the environment with `python -m venv` instead, `python -m pip` is
available, but the hashed lock and `--only-binary :all:` review still apply.

### Linux (bash)

```bash
uv venv .venv --python python3.13
uv pip sync --python .venv/bin/python --require-hashes --only-binary :all: --index-url https://pypi.org/simple requirements-dev.lock
export PYTHONPATH="$PWD/src"
.venv/bin/python -m agentwriter --help
```

For a runtime-only installation, sync `requirements.lock` instead of
`requirements-dev.lock`. Do not install from `requirements.in` for an accepted
run; the `.lock` files are the reviewed, hash-pinned sets.

## Configure the process

Copy the safe local example and keep the real file private:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

```bash
test -e .env || cp .env.example .env
```

The application does **not** discover `.env` automatically. Pass it explicitly
with the global `--env-file` option, or export the variables in the process
that starts the API and worker:

```powershell
.\.venv\Scripts\python.exe -m agentwriter --env-file .env settings
```

```bash
.venv/bin/python -m agentwriter --env-file .env settings
```

`settings` prints a sanitized summary. It never prints credentials or the
database URL. Environment variables override values in the dotenv file.

The fixture defaults are:

```
APP_ENV=development
DEVELOPMENT_MODE=true
AUTH_MODE=local
BIND_HOST=127.0.0.1
MODEL_PROVIDER=fixture
RESEARCH_PROVIDER=fixture
ENABLE_EXTERNAL_CALLS=false
MODEL_POLICY=free_only
```

Local authentication is a single-owner development identity. It is rejected on
public bind addresses and in staging/production. Do not expose this profile to
an untrusted network. Hosted OIDC is not implemented yet; `AUTH_MODE=oidc`
fails closed.

## Create PostgreSQL

AgentWriter has no SQLite or in-memory fallback. The API and worker require a
real PostgreSQL database with the application migrations applied.

### Option A: an existing PostgreSQL 18 server

Ask the database administrator to create two roles and a database. The
migration role owns the schema; the runtime role is restricted:

```sql
CREATE ROLE agentwriter_migrate LOGIN PASSWORD 'replace-me';
CREATE ROLE agentwriter_runtime LOGIN PASSWORD 'replace-me';
CREATE DATABASE agentwriter OWNER agentwriter_migrate;
```

Use URL-encoded credentials in the DSNs. Keep both passwords in an ignored
`.env` file or a secret manager, never in Git or shell history.

### Option B: a disposable local container

```powershell
docker run --name agentwriter-postgres --rm -d `
  -e POSTGRES_PASSWORD=local-only-password `
  -e POSTGRES_DB=agentwriter `
  -p 127.0.0.1:5432:5432 postgres:18
```

```bash
docker run --name agentwriter-postgres --rm -d \
  -e POSTGRES_PASSWORD=local-only-password \
  -e POSTGRES_DB=agentwriter \
  -p 127.0.0.1:5432:5432 postgres:18
```

For a disposable local database the container user may own the schema. For a
shared or production-shaped database, use the two-role setup above.

## Apply migrations

Set the migration and runtime DSNs, then run the migration command once per
deployment. The API and worker do not run DDL at startup.

```powershell
$env:MIGRATION_DATABASE_URL = 'postgresql://agentwriter_migrate:replace-me@127.0.0.1:5432/agentwriter'
$env:DATABASE_URL = 'postgresql://agentwriter_runtime:replace-me@127.0.0.1:5432/agentwriter'
.\.venv\Scripts\python.exe -m agentwriter.storage.migrate --directory migrations --runtime-role agentwriter_runtime
```

```bash
export MIGRATION_DATABASE_URL='postgresql://agentwriter_migrate:replace-me@127.0.0.1:5432/agentwriter'
export DATABASE_URL='postgresql://agentwriter_runtime:replace-me@127.0.0.1:5432/agentwriter'
.venv/bin/python -m agentwriter.storage.migrate --directory migrations --runtime-role agentwriter_runtime
```

The command takes an advisory lock, verifies applied migration checksums,
applies missing migrations transactionally, initializes the LangGraph
checkpoint tables, and grants data access to the runtime role. It is safe to
repeat while migration contents match. A changed, missing or out-of-order
historical migration fails; add a new migration instead of editing one that
has already run. `--application-only` skips checkpoint setup and is for focused
database tests, not a complete deployment.

If the database already has migrations from an older revision, restore the
matching backup or add a new forward migration. Do not delete migration rows or
edit an applied file to make startup pass.

## Start the API and worker

Use two terminals. Both processes read the same `DATABASE_URL`; the worker is
the only process that executes runs and calls providers.

```powershell
# Terminal 1
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
.\.venv\Scripts\python.exe -m agentwriter --env-file .env serve

# Terminal 2
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
.\.venv\Scripts\python.exe -m agentwriter --env-file .env worker
```

```bash
# Terminal 1
export PYTHONPATH="$PWD/src"
.venv/bin/python -m agentwriter --env-file .env serve

# Terminal 2
export PYTHONPATH="$PWD/src"
.venv/bin/python -m agentwriter --env-file .env worker
```

Open `http://127.0.0.1:8000`. `Ctrl+C` stops each process. A run survives a
browser close or API restart; stop the worker only when you intend to pause
processing.

### Probes and metrics

The API exposes:

| Endpoint | Purpose | Expected |
|---|---|---|
| `/live` | Process liveness | `200 {"status":"alive"}` |
| `/ready` | Database reachability and a fresh worker heartbeat | `200 {"status":"ready"}` or `503` |
| `/metrics` | Prometheus text metrics | Loopback access, or `Authorization: Bearer $METRICS_TOKEN` |

```powershell
Invoke-RestMethod http://127.0.0.1:8000/live
Invoke-RestMethod http://127.0.0.1:8000/ready
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/metrics | Select-Object -ExpandProperty Content
```

```bash
curl -fsS http://127.0.0.1:8000/live
curl -fsS http://127.0.0.1:8000/ready
curl -fsS http://127.0.0.1:8000/metrics
```

`/ready` returns `503` until the database is reachable and at least one worker
heartbeat is fresh (`WORKER_STALE_SECONDS`, 60 by default). Start the worker
before treating a `503` as a database fault. `/metrics` is loopback-only unless
`METRICS_TOKEN` is set; then every request must present the bearer token.

## Tests and static checks

The unit suite needs no database or provider. The integration suite needs a
disposable PostgreSQL database whose role can create and drop schemas; it
creates a unique schema per test and removes it.

```powershell
$env:TEST_DATABASE_URL = 'postgresql://agentwriter_test:replace-me@127.0.0.1:5432/agentwriter_test'
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts
.\.venv\Scripts\python.exe -m mypy src
```

```bash
export TEST_DATABASE_URL='postgresql://agentwriter_test:replace-me@127.0.0.1:5432/agentwriter_test'
export PYTHONPATH="$PWD/src"
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -m mypy src
```

Without `TEST_DATABASE_URL`, database tests are visibly skipped. A configured
but unavailable database fails; it does not fall back to SQLite or mocks. A
green unit run is not database acceptance.

## Evaluation and smoke checks

The versioned snapshot corpus runs the production graph assembly with fixture
providers. It needs no database or network:

```powershell
.\.venv\Scripts\python.exe scripts\evaluate.py --baseline evaluation\corpus\v1\baseline.json
```

```bash
.venv/bin/python scripts/evaluate.py --baseline evaluation/corpus/v1/baseline.json
```

The accepted result is 38/38 cases and 346/346 checks with result hash
`1cf921c8b8e5f6759e6331e8b97d1a6d726dd2acf8d1eaae76242e4fa4a989f3`. The
baseline comparison fails if the result changes. Do not edit the baseline
without reviewing the change. The 16 source-dependent entries in `live.jsonl`
are not executed by the fixture evaluator; they require qualified live
providers.

A database-free smoke test proves the packaged fixture entry point, synthetic
disclosure and citation marker:

```powershell
.\.venv\Scripts\python.exe scripts\smoke.py
```

```bash
.venv/bin/python scripts/smoke.py
```

## Check a running local fixture profile

Start the API and worker with both providers set to `fixture` and
`ENABLE_EXTERNAL_CALLS=false`. With the current source running, use:

```powershell
.\.venv\Scripts\python.exe scripts\check_local.py --url http://127.0.0.1:8000 --runs 5 --output .local\local-check.json
```

The check requires a literal loopback address, a ready worker and the explicit
fixture-only session profile. It creates its own runs, reopens them, verifies
download hashes and synthetic labels, replays committed progress, checks the
terminal 204 response, then deletes only those test runs and its own session.
Reports contain counts/timings, not article text, credentials or session tokens.
Use a new output filename for each check and close other progress views first.

Use a separate disposable database/profile for a 100-run soak. Fixture calls
still consume the application's daily call count, so the default daily ceiling
may stop a long batch. Set an adequate `USER_DAILY_CALLS` only in that disposable
fixture worker's profile (the recorded 100-run check used 10000), then select
`--runs 100`. Do not raise live-provider quotas or budgets for this check. Local
fixture timings do not establish live-model quality or deployed capacity.

Before staging or publishing changes, check the repository boundary:

```powershell
.\.venv\Scripts\python.exe scripts\check_publication.py --worktree
# After staging only the reviewed public files:
.\.venv\Scripts\python.exe scripts\check_publication.py
```

The first command checks current and untracked nonignored files; the second
checks exact index bytes. Both reject internal paths and common token/private-key
patterns. They complement `.gitignore` and a content review; they are not a full
security audit. Internal plans, trackers, evidence and backups stay local.

## Docker Compose profile

Compose runs PostgreSQL, a one-shot migration, a Caddy loopback proxy, the API
and the worker. The database is not published to the host; only
`127.0.0.1:8000` is exposed.

```powershell
Copy-Item .env.example .env
# Set POSTGRES_PASSWORD to a local-only value in .env.
$env:POSTGRES_PASSWORD = 'local-only-password'
docker compose up -d --build
docker compose ps
docker compose logs --tail 50 api worker
Invoke-RestMethod http://127.0.0.1:8000/live
Invoke-RestMethod http://127.0.0.1:8000/ready
```

```bash
cp .env.example .env
# Set POSTGRES_PASSWORD to a local-only value in .env.
export POSTGRES_PASSWORD='local-only-password'
docker compose up -d --build
docker compose ps
docker compose logs --tail 50 api worker
curl -fsS http://127.0.0.1:8000/live
curl -fsS http://127.0.0.1:8000/ready
```

Compose reads `.env` automatically for variable substitution; the Python CLI
still needs `--env-file`. `POSTGRES_PASSWORD` is mandatory and Compose fails
fast if it is missing. The `migrate` service must exit `0` before the API and
worker start. Stop the stack with:

```powershell
docker compose down
```

`docker compose down -v` also deletes the database volume. Use it only for a
disposable local profile. The API and worker containers run read-only with
dropped capabilities; the Caddy container owns the network namespace so the API
binds only to loopback inside it.

## Backup, restore and rollback

Back up before changing migrations, retention bounds or a provider profile.
The local profile uses an encrypted logical backup:

The shell inside the database container uses `pipefail`: an unsuccessful dump
must fail the backup even if encryption itself succeeds. Keep encrypted dumps
and their checksums in the ignored `backups/` directory.

From PowerShell, first supply `BACKUP_PASSPHRASE` securely in the process
environment, then:

```powershell
New-Item -ItemType Directory -Force backups | Out-Null
$backupName = 'agentwriter-' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ') + '.dump.enc'
docker exec -e BACKUP_PASSPHRASE agentwriter-db-1 bash -o pipefail -ec 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom | openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:BACKUP_PASSPHRASE -out /tmp/agentwriter.dump.enc'
if ($LASTEXITCODE -ne 0) { throw 'Backup failed; do not use the partial output.' }
docker cp agentwriter-db-1:/tmp/agentwriter.dump.enc (Join-Path backups $backupName)
if ($LASTEXITCODE -ne 0) { throw 'Backup copy failed; retain the container copy.' }
Get-FileHash (Join-Path backups $backupName) -Algorithm SHA256
docker exec agentwriter-db-1 rm -f -- /tmp/agentwriter.dump.enc
```

From a Linux shell with the corresponding process variables set:

```bash
mkdir -p backups
docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" -e BACKUP_PASSPHRASE="$BACKUP_PASSPHRASE" \
  agentwriter-db-1 bash -o pipefail -ec \
  'pg_dump -U agentwriter -d agentwriter --format=custom \
   | openssl enc -aes-256-cbc -pbkdf2 -salt \
       -pass env:BACKUP_PASSPHRASE -out /tmp/agentwriter.dump.enc'
docker cp agentwriter-db-1:/tmp/agentwriter.dump.enc \
  ./backups/agentwriter-$(date +%Y%m%dT%H%M%SZ).dump.enc
docker exec agentwriter-db-1 rm -f /tmp/agentwriter.dump.enc
```

Store the encrypted file and its checksum off-host. Never commit a dump,
passphrase or host path. Restore into a separate database first:

```bash
docker exec agentwriter-db-1 psql -U agentwriter -d postgres \
  -c "CREATE DATABASE agentwriter_restore"
docker cp backups/agentwriter-20261024T000000Z.dump.enc \
  agentwriter-db-1:/tmp/restore.dump.enc
docker exec -e BACKUP_PASSPHRASE="$BACKUP_PASSPHRASE" agentwriter-db-1 bash -o pipefail -ec \
  'openssl enc -d -aes-256-cbc -pbkdf2 -pass env:BACKUP_PASSPHRASE \
     -in /tmp/restore.dump.enc \
   | pg_restore -U agentwriter -d agentwriter_restore \
       --no-owner --no-privileges --exit-on-error'
```

Verify before cutover:

```sql
SELECT id, state, current_revision, deleted_at FROM runs;
SELECT count(*) FROM article_revisions;
SELECT count(*) FROM checkpoints;
SELECT count(*) FROM checkpoint_writes;
SELECT count(*) FROM run_events;
SELECT count(*) FROM sources;
```

Compare the counts and current revision hashes with the source database. A
restore is not accepted if the article hash, checkpoint count or deletion
tombstones differ. For cutover, stop the API and worker, restore or rename the
database, run the matching migrations, start the worker, then the API, verify
`/ready`, and complete one fixture run. Keep the previous volume until the
restored stack passes.

Deletion is a tombstone. A deleted run remains hidden, returns `404` on direct
access and `410` for its idempotency key, and is removed by the retention job
after `DELETED_RUN_GRACE_HOURS` (24 by default). Retention is not a backup.

## Change settings safely

1. Edit the ignored `.env` or the service environment.
2. Run `python -m agentwriter --env-file .env settings` and confirm the
   sanitized summary.
3. Restart the API and worker so both use the same values.
4. Verify `/ready` and one fixture run.
5. For retention changes, stop the retention timer first, apply the new
   environment, run `python -m agentwriter retention --batch-size 100` once
   manually, confirm the JSON counters and the active queue, then re-enable the
   timer.

Do not put a DSN or provider key on a command line. The CLI's error messages
identify an invalid setting without printing its value.

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `Set DATABASE_URL ...` | No runtime DSN in the process | Set `DATABASE_URL` or pass `--env-file .env`. |
| `No module named agentwriter` | `PYTHONPATH` is unset | Set `PYTHONPATH` to the repository `src` directory. |
| `No module named pip` | The venv was created by `uv` | Use `uv pip sync ...`, not `python -m pip`. |
| Migration checksum or order error | An applied migration was edited or is missing | Restore the matching backup or add a new forward migration; do not rewrite history. |
| `/ready` returns `503` | Database unavailable, migrations missing or no fresh worker | Check the DSN, run migrations, start the worker and read its logs. |
| Worker starts but runs stay queued | API and worker use different databases or the worker is stale | Compare `DATABASE_URL`, check `/metrics` for `agentwriter_workers`, and read worker logs. |
| `POSTGRES_PASSWORD is missing` | Compose variable is not set | Set it in `.env` or the shell before `docker compose`. |
| Port `8000` is already in use | Another API or Compose stack is running | Stop the other process or change `PORT`/the Compose mapping. |
| Database tests are skipped | `TEST_DATABASE_URL` is unset | Set it to a disposable database whose role can create schemas. |
| Evaluation baseline mismatch | A graph or corpus change altered the result | Inspect the result hash and review the change; do not edit the baseline blindly. |
| Browser returns `401` | The local session expired or the API restarted | Reload the page. |
| Browser returns `403` on a mutation | Stale CSRF token or a cross-origin request | Reload and use the app's own origin. |
| Compose logs show a migration failure | The volume has an incompatible schema history | Stop the stack, restore a matching backup, then rerun `migrate`. |

## Boundaries

This profile is local and fixture-only. It does not qualify live OpenRouter or
Treg behavior, hosted OIDC identity, human factual review, deployed load or
fault behavior, or release acceptance. The manual screen-reader sample for the
browser is still a pre-public-release item. See the root
[README](../README.md#local-use-and-providers) for the project overview and local-use guidance.
