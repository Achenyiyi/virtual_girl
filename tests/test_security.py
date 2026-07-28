"""Tests for security controls applied to persisted diagnostics."""

from __future__ import annotations

import logging

import pytest

from companion.security.redaction import RedactingFormatter, redact_text


@pytest.mark.parametrize(
    "secret",
    [
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        "Bearer abcdefghijklmnopqrstuvwxyz",
        "api_key=abcdefghijklmnopqrstuvwxyz",
        "token: abcdefghijklmnopqrstuvwxyz",
        "sk-abcdefghijklmnopqrstuvwxyz123456",
    ],
)
def test_redact_text_removes_credentials(secret: str) -> None:
    output = redact_text(f"request failed: {secret}")
    assert "abcdefghijklmnopqrstuvwxyz" not in output
    assert "[REDACTED]" in output


def test_formatter_redacts_interpolated_arguments() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="provider failed with api_key=%s",
        args=("abcdefghijklmnopqrstuvwxyz",),
        exc_info=None,
    )
    output = RedactingFormatter("%(message)s").format(record)
    assert output == "provider failed with api_key=[REDACTED]"
