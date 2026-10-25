"""Structured log formatters must redact secrets before any handler sees them."""

import logging

from fieldnote.logging_config import JsonFormatter, RedactionFilter, TextFormatter, redact


def test_redaction_removes_credentials_dsns_bearer_tokens_and_cookies() -> None:
    message = (
        "Authorization: Bearer abc.def.ghi "
        "api_key=super-secret password=hunter2 "
        "postgresql://alice:db-password@db:5432/fieldnote "
        "Cookie: aw_session=session-token X-CSRF-Token: csrf-token"
    )
    cleaned = redact(message)
    for secret in ("abc.def.ghi", "super-secret", "hunter2", "db-password", "session-token", "csrf-token"):
        assert secret not in cleaned
    assert "[REDACTED]" in cleaned


def test_formatters_emit_no_secret_and_keep_correlation_id() -> None:
    record = logging.LogRecord(
        "fieldnote.test",
        logging.ERROR,
        __file__,
        1,
        "provider failed with api_key=provider-secret at postgresql://u:p@db/x",
        (),
        None,
    )
    record.correlation_id = "corr-123"
    for formatter in (JsonFormatter(), TextFormatter()):
        output = formatter.format(record)
        assert "provider-secret" not in output
        assert "postgresql://u:p@db/x" not in output
        assert "corr-123" in output


def test_redaction_filter_mutates_message_and_args_before_handlers() -> None:
    record = logging.LogRecord(
        "fieldnote.test",
        logging.INFO,
        __file__,
        1,
        "token=%s dsn=%s",
        ("arg-secret", "postgresql://u:p@db/x"),
        None,
    )
    assert RedactionFilter().filter(record) is True
    output = record.getMessage()
    assert "arg-secret" not in output
    assert "u:p@db/x" not in output
    assert "[REDACTED]" in output
