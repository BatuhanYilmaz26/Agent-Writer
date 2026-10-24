import asyncio
import socket
from unittest.mock import AsyncMock

import pytest

from agentwriter.services.urls import (
    UnsafeURL,
    canonicalize_url,
    resolve_public_url,
    validate_public_url,
)


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost/path",
        "https://x.local/path",
        "https://x.internal/",
        "https://127.0.0.1/",
        "http://127.1/",
        "http://2130706433/",
        "http://0x7f000001/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "https://user:password@example.com/",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "https://example.com:8443/",
        "https://example.com/\nattack",
        "https://example.com\\@localhost/",
        "https://example.com/>)<img>",
        "https://example.com/`data`",
    ],
)
def test_source_url_rejects_nonpublic_and_markup_targets(value):
    with pytest.raises(UnsafeURL):
        validate_public_url(value)


def test_normalization_preserves_semantic_query_and_drops_tracking_only():
    assert (
        validate_public_url("HTTPS://Example.COM:443/page?q=first&q=second#section")
        == "https://example.com/page?q=first&q=second#section"
    )
    assert (
        canonicalize_url("https://EXAMPLE.com/article?b=2&utm_source=x&a=1&b=3#part")
        == "https://example.com/article?b=2&a=1&b=3"
    )


@pytest.mark.asyncio
async def test_dns_preflight_rejects_mixed_public_private_answers(monkeypatch):
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop,
        "getaddrinfo",
        AsyncMock(
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
            ]
        ),
    )
    with pytest.raises(UnsafeURL, match="non-public"):
        await resolve_public_url("https://example.com/")


@pytest.mark.asyncio
async def test_dns_preflight_returns_qualified_addresses_without_fetching(monkeypatch):
    monkeypatch.setattr(
        asyncio.get_running_loop(),
        "getaddrinfo",
        AsyncMock(
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            ]
        ),
    )
    assert await resolve_public_url("https://example.com/") == (
        "https://example.com/",
        ("93.184.216.34",),
    )
