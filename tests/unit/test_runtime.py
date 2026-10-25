"""Event-loop entry points must be usable by both asyncio.Runner and Uvicorn."""

import asyncio

from fieldnote.runtime import loop_factory


def test_loop_factory_returns_a_closable_selector_loop() -> None:
    loop = loop_factory()
    try:
        assert isinstance(loop, asyncio.AbstractEventLoop)
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()
