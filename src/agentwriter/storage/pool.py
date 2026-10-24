"""Explicitly opened async connection pools with fail-closed schema checks."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .migrate import verify_schema


@asynccontextmanager
async def database_pool(
    database_url: str,
    *,
    max_size: int = 8,
    timeout: float = 5.0,
    migrations_directory: Path = Path("migrations"),
) -> AsyncIterator[AsyncConnectionPool[AsyncConnection[dict[str, Any]]]]:
    """Open, verify, yield, and close even if startup fails partway."""
    pool = AsyncConnectionPool[AsyncConnection[dict[str, Any]]](
        database_url,
        min_size=1,
        max_size=max_size,
        timeout=timeout,
        open=False,
        kwargs={"autocommit": True, "row_factory": dict_row, "connect_timeout": 5},
    )
    try:
        await pool.open()
        await pool.wait(timeout=timeout)
        async with pool.connection() as connection:
            await verify_schema(connection, migrations_directory)
            await connection.execute("SELECT 1 FROM runs LIMIT 1")
        yield pool
    finally:
        await pool.close()
