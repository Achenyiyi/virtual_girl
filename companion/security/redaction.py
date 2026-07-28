"""Deterministic credential redaction for user-visible and persisted logs."""

from __future__ import annotations

import logging
import re
from typing import Any

_REDACTED = "[REDACTED]"
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(
        r"(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*)"
        r"[^\s,;]+"
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
)


def redact_text(value: object) -> str:
    """Remove common credential forms without exposing the matched value."""
    text = str(value)
    for pattern in _PATTERNS:
        text = pattern.sub(
            lambda match: f"{match.group(1)}{_REDACTED}" if match.lastindex else _REDACTED, text
        )
    return text


def redact_mapping(value: Any, key: str = "") -> Any:
    """Recursively redact credentials and private message-like fields."""
    sensitive_fragments = (
        "password",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "message",
        "body",
        "content",
    )
    if key and any(fragment in key.lower() for fragment in sensitive_fragments):
        return _REDACTED
    if isinstance(value, dict):
        return {
            str(item_key): redact_mapping(item_value, str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [redact_mapping(item) for item in value]
    if isinstance(value, tuple):
        return [redact_mapping(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class RedactingFormatter(logging.Formatter):
    """Apply credential redaction after normal logging interpolation."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))
