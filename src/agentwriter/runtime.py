"""Cross-platform event loop entry points for Psycopg async connections."""

import asyncio


def loop_factory(use_subprocess: bool = False):
    """Uvicorn custom loop factory; avoid Windows Proactor/Psycopg incompatibility."""
    del use_subprocess
    return asyncio.SelectorEventLoop


def run_async(awaitable):
    return asyncio.run(awaitable, loop_factory=asyncio.SelectorEventLoop)
