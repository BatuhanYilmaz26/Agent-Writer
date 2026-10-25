"""Shared disposable-PostgreSQL fixtures for application-level acceptance."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import httpx
import psycopg
import pytest
import pytest_asyncio
from psycopg import sql

from fieldnote.main import create_app
from fieldnote.settings import Settings
from fieldnote.storage.migrate import migrate

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
TEST_DSN = os.environ.get("TEST_DATABASE_URL")


@asynccontextmanager
async def scoped_database(
    *, setup_saver: bool = True, migrations_directory: Path = MIGRATIONS,
) -> AsyncIterator[str]:
    """Create an isolated schema and drop it afterwards."""
    if not TEST_DSN:
        pytest.skip(
            "TEST_DATABASE_URL is not configured; real PostgreSQL acceptance remains unverified"
        )
    schema = "aw_accept_" + uuid4().hex
    admin = await psycopg.AsyncConnection.connect(TEST_DSN, autocommit=True)
    await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    parsed = urlsplit(TEST_DSN)
    scoped_url = urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode({"options": f"-c search_path={schema},public"}, quote_via=quote),
            "",
        )
    )
    try:
        await migrate(scoped_url, migrations_directory, setup_saver=setup_saver)
        yield scoped_url
    finally:
        await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        await admin.close()


@pytest_asyncio.fixture
async def acceptance_database() -> AsyncIterator[str]:
    async with scoped_database() as url:
        yield url


@pytest_asyncio.fixture
async def api_client(acceptance_database: str) -> AsyncIterator[tuple[httpx.AsyncClient, object]]:
    settings = Settings(
        _env_file=None,
        database_url=acceptance_database,
        allowed_hosts=("127.0.0.1", "localhost", "::1", "testserver"),
        max_user_nonterminal_runs=10,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/api/v1/session")
            assert response.status_code == 200
            client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
            yield client, app

