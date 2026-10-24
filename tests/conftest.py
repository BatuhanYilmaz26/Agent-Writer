"""Use Psycopg-compatible selector loops on Windows and Unix."""

import asyncio


def pytest_asyncio_loop_factories():
    return {"selector": asyncio.SelectorEventLoop}
