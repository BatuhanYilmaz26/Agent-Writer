"""Explicit loopback-only development sessions; hosted OIDC is fail-closed."""

import hashlib
import hmac
import ipaddress
import secrets
import time
from urllib.parse import urlsplit

from fastapi import Request

from agentwriter.storage.repository import StorageError


def local_principal(request: Request) -> str:
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


def issue_session(request: Request) -> tuple[str, str]:
    local_principal(request)
    nonce = secrets.token_urlsafe(24)
    body = f"{int(time.time())}.{nonce}"
    signature = hmac.new(request.app.state.local_session_key, body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}", nonce


def authenticated(request: Request) -> str:
    owner = local_principal(request)
    token = request.cookies.get("aw_session", "")
    try:
        timestamp, nonce, signature = token.split(".")
        age = time.time() - int(timestamp)
        expected = hmac.new(
            request.app.state.local_session_key, f"{timestamp}.{nonce}".encode(), hashlib.sha256,
        ).hexdigest()
        valid = 0 <= age <= 8 * 3600 and hmac.compare_digest(signature, expected)
    except (ValueError, TypeError):
        valid = False
        nonce = ""
    if not valid:
        raise StorageError("session_required", "Open the app to start a local session.", 401)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        csrf = request.headers.get("x-csrf-token", "")
        if not csrf or not hmac.compare_digest(csrf, nonce):
            raise StorageError("csrf_rejected", "Reload the page before trying this action.", 403)
        origin = request.headers.get("origin")
        if origin:
            actual = urlsplit(str(request.base_url))
            parsed = urlsplit(origin)
            if (parsed.scheme, parsed.netloc) != (actual.scheme, actual.netloc):
                raise StorageError("origin_rejected", "This request came from another site.", 403)
    return owner
