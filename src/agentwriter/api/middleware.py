"""Bound incoming bodies and add a strict browser response policy."""

import json
import re
from uuid import uuid4

# 200,000 characters can occupy 1.2 MB as JSON Unicode escapes. Only the
# immutable-edit endpoint receives this envelope; briefs retain their limit.
MAX_EDIT_BYTES = 1_310_720
EDIT_PATH = re.compile(r"^/api/v1/runs/[0-9a-fA-F-]{36}/revisions$")


class BoundaryMiddleware:
    def __init__(self, app, max_bytes=32768):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        correlation_id = str(uuid4())
        scope.setdefault("state", {})["correlation_id"] = correlation_id

        async def guarded_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend([
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                    (b"x-correlation-id", correlation_id.encode()),
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"),
                ])
                message["headers"] = headers
            await send(message)

        if scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
            editing = scope["method"] == "POST" and EDIT_PATH.fullmatch(scope["path"])
            limit = MAX_EDIT_BYTES if editing else self.max_bytes
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > limit:
                    description = "The saved edit is too large." if editing else "The writing brief is too large."
                    payload = json.dumps({"code": "body_too_large", "message": description,
                                          "retryable": False, "correlation_id": correlation_id}).encode()
                    await guarded_send({"type": "http.response.start", "status": 413,
                                "headers": [(b"content-type", b"application/json")]})
                    await send({"type": "http.response.body", "body": payload})
                    return
                if not message.get("more_body", False):
                    break
            delivered = False
            original_receive = receive

            async def buffered_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await original_receive()

            receive = buffered_receive

        await self.app(scope, receive, guarded_send)
