"""Database-backed loopback sessions; hosted OIDC remains fail-closed.

Only SHA-256 digests of the cookie token and CSRF token are persisted. The CSRF
value is derived from the raw cookie token with a process/config key so several
tabs share one stable value, while a process restart can rotate it safely.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
from urllib.parse import urlsplit

from fastapi import Request

from fieldnote.storage.repository import StorageError

SESSION_COOKIE = "aw_session"
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,200}$")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _csrf_key(request: Request) -> bytes:
    settings = request.app.state.settings
    if settings.session_secret is not None:
        return settings.session_secret.get_secret_value().encode("utf-8")
    return request.app.state.local_session_key


def csrf_for(request: Request, token: str) -> str:
    """Derive a stable CSRF token from the opaque session cookie."""
    return hmac.new(_csrf_key(request), f"csrf:{token}".encode(), hashlib.sha256).hexdigest()


def local_principal(request: Request) -> str:
    """Authenticate the development principal without trusting forwarded headers."""
    settings = request.app.state.settings
    if settings.auth_mode != "local":
        raise StorageError("authentication_unavailable", "Hosted identity is not configured.", 503)
    try:
        peer = request.client.host if request.client else ""
        loopback = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        loopback = False
    if not loopback or not settings.development_mode:
        raise StorageError("local_only", "Development access is limited to this computer.", 403)
    return settings.local_owner_id


def _origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    actual = urlsplit(str(request.base_url))
    parsed = urlsplit(origin)
    return (parsed.scheme, parsed.netloc) == (actual.scheme, actual.netloc)


async def create_session(request: Request) -> tuple[str, str]:
    """Create a persisted session and return its cookie/CSRF tokens."""
    owner = local_principal(request)
    if request.method not in {"GET", "HEAD", "OPTIONS"} and not _origin_allowed(request):
        raise StorageError("origin_rejected", "This request came from another site.", 403)
    token = secrets.token_urlsafe(32)
    csrf = csrf_for(request, token)
    await request.app.state.repository.create_session(
        owner,
        _digest(token),
        _digest(csrf),
        request.app.state.settings.session_ttl_hours,
    )
    return token, csrf


async def authenticated(request: Request) -> str:
    """Resolve the session on every request; mutations also require CSRF/Origin."""
    owner = local_principal(request)
    token = request.cookies.get(SESSION_COOKIE, "")
    if not _SAFE_TOKEN.fullmatch(token):
        raise StorageError("session_required", "Open the app to start a local session.", 401)
    session = await request.app.state.repository.get_session(_digest(token))
    if session is None or session["owner_id"] != owner:
        raise StorageError("session_required", "Your session expired. Reload the app.", 401)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        csrf = request.headers.get("x-csrf-token", "")
        if not csrf or not hmac.compare_digest(_digest(csrf), session["csrf_hash"]):
            raise StorageError("csrf_rejected", "Reload the page before trying this action.", 403)
        if not _origin_allowed(request):
            raise StorageError("origin_rejected", "This request came from another site.", 403)
    return owner


async def refresh_csrf(request: Request) -> str:
    """Return the derived CSRF token and repair its digest after a key rotation."""
    await authenticated(request)
    token = request.cookies.get(SESSION_COOKIE, "")
    csrf = csrf_for(request, token)
    updated = await request.app.state.repository.set_session_csrf(_digest(token), _digest(csrf))
    if not updated:
        raise StorageError("session_required", "Your session expired. Reload the app.", 401)
    return csrf


async def revoke_current(request: Request) -> None:
    """Revoke the presented session after CSRF validation."""
    await authenticated(request)
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        await request.app.state.repository.revoke_session(_digest(token))
