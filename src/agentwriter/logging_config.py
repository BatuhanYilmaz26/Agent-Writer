"""Bounded structured logging with explicit secret redaction.

Log records never include request bodies, provider payloads, cookies, database
DSNs or bearer tokens. The formatter is deliberately independent of any vendor
so the same output works for the API, worker, migration and retention commands.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

_REDACTIONS = (
    (re.compile(r"(?i)(authorization|cookie|x-csrf-token|x-treg-token)\s*[:=]\s*\S+"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+"), r"\1=[REDACTED]"),
    (re.compile(r"postgres(?:ql)?://[^\s\"']+"), "postgresql://[REDACTED]"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    (re.compile(r"aw_session=[^;\s]+"), "aw_session=[REDACTED]"),
)


def redact(value: str) -> str:
    result = value
    for pattern, replacement in _REDACTIONS:
        result = pattern.sub(replacement, result)
    return result


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        correlation = getattr(record, "correlation_id", None)
        if correlation:
            payload["correlation_id"] = str(correlation)
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{datetime.fromtimestamp(record.created, tz=UTC).isoformat()} {record.levelname} {record.name}: {redact(record.getMessage())}"
        correlation = getattr(record, "correlation_id", None)
        if correlation:
            base += f" correlation_id={correlation}"
        if record.exc_info and record.exc_info[0] is not None:
            base += f" exception_type={record.exc_info[0].__name__}"
        return base


class RedactionFilter(logging.Filter):
    """Apply redaction before any handler, including third-party handlers."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            record.args = tuple(redact(arg) if isinstance(arg, str) else arg for arg in record.args)
        return True


def configure_logging(settings) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if settings.log_format == "json" else TextFormatter())
    handler.addFilter(RedactionFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
