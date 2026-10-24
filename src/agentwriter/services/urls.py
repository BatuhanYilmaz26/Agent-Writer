"""Public research-link policy; this module does not fetch URLs.

The syntactic policy rejects private/literal targets. DNS validation is an
additional preflight only; a future direct fetcher must enforce the policy at
connection time and across redirects to prevent DNS rebinding.
"""

import asyncio
import ipaddress
import re
import socket
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_DENIED_SUFFIXES = (".localhost", ".local", ".internal", ".home", ".test", ".invalid", ".example")
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_TRACKING_KEYS = {"gclid", "fbclid", "msclkid"}


class UnsafeURL(ValueError):
    """A link does not satisfy the public-source policy."""


def validate_public_url(value: str) -> str:
    """Return a normalized public DNS HTTP(S) URL without making a request.

    IP literals, unusual ports, credentials, local hostnames and legacy numeric
    IPv4 spellings are rejected even when a particular library accepts them.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise UnsafeURL("Source URL length is invalid")
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in value) or any(ch in value for ch in '\\<>"`'):
        raise UnsafeURL("Source URL contains forbidden characters")
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
            raise UnsafeURL("Only public HTTP(S) sources are permitted")
        if parts.username is not None or parts.password is not None:
            raise UnsafeURL("Source URLs cannot contain credentials")
        raw_host = parts.hostname or ""
        host = raw_host.rstrip(".").encode("idna").decode("ascii").lower()
        port = parts.port
    except (ValueError, UnicodeError) as exc:
        raise UnsafeURL("Invalid source URL") from exc
    if (
        not host
        or len(host) > 253
        or "." not in host
        or host == "localhost"
        or host.endswith(_DENIED_SUFFIXES)
    ):
        raise UnsafeURL("Source must use a public DNS hostname")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise UnsafeURL("IP-literal source URLs are disabled")
    labels = host.split(".")
    if not all(_LABEL.fullmatch(label) for label in labels) or labels[-1].isdigit():
        raise UnsafeURL("Invalid public DNS hostname")
    if any(label.startswith("0x") for label in labels) and all(
        re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)", label) for label in labels
    ):
        raise UnsafeURL("Numeric source hostnames are disabled")
    scheme = parts.scheme.lower()
    if port is not None and port != {"http": 80, "https": 443}[scheme]:
        raise UnsafeURL("Only standard public web ports are permitted")
    return urlunsplit((scheme, host, parts.path or "/", parts.query, parts.fragment))


def canonicalize_url(value: str) -> str:
    """Remove fragment and known marketing keys without reordering query values."""
    parts = urlsplit(validate_public_url(value))
    query = [
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_KEYS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


async def resolve_public_url(value: str) -> tuple[str, tuple[str, ...]]:
    """Resolve a candidate and reject any non-global address; never fetch it."""
    normalized = validate_public_url(value)
    parts = urlsplit(normalized)
    loop = asyncio.get_running_loop()
    try:
        records = await asyncio.wait_for(
            loop.getaddrinfo(
                parts.hostname, 443 if parts.scheme == "https" else 80, type=socket.SOCK_STREAM
            ),
            timeout=5.0,
        )
        addresses = tuple(sorted({str(record[4][0]) for record in records}))
        if not addresses or any(not ipaddress.ip_address(addr).is_global for addr in addresses):
            raise UnsafeURL("Source hostname resolves to a non-public address")
    except (OSError, TimeoutError, ValueError) as exc:
        if isinstance(exc, UnsafeURL):
            raise
        raise UnsafeURL("Public source hostname could not be qualified") from exc
    return normalized, addresses
