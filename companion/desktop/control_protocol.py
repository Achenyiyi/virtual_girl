"""Strict envelopes and validation for Companion Control Protocol v1."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

CONTROL_PROTOCOL = "companion-control"
CONTROL_VERSION = 1
MAX_MESSAGE_BYTES = 256 * 1024
MAX_REQUEST_ID_LENGTH = 128
MAX_CONCURRENT_REQUESTS = 8
DEFAULT_RPC_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class ControlRequest:
    request_id: str
    method: str
    params: dict[str, Any]


class ControlError(Exception):
    """A renderer-safe protocol error."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def decode_request(message: str | bytes) -> ControlRequest:
    """Decode one bounded JSON request without reflecting untrusted input."""
    if isinstance(message, bytes):
        raw = message
        try:
            text = message.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ControlError("invalid_json", "Message must be UTF-8 JSON.") from exc
    else:
        text = message
        raw = message.encode("utf-8")
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ControlError("message_too_large", "Message exceeds the size limit.")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ControlError("invalid_json", "Message must be valid JSON.") from exc
    if not isinstance(value, dict):
        raise ControlError("invalid_request", "Request must be a JSON object.")
    if value.get("protocol") != CONTROL_PROTOCOL or value.get("version") != CONTROL_VERSION:
        raise ControlError("unsupported_protocol", "Unsupported control protocol version.")
    if value.get("type") != "request":
        raise ControlError("invalid_request", "Message type must be request.")
    request_id = value.get("id")
    if (
        not isinstance(request_id, str)
        or not request_id
        or len(request_id) > MAX_REQUEST_ID_LENGTH
        or any(ord(character) < 32 for character in request_id)
    ):
        raise ControlError("invalid_request", "Request id is invalid.")
    method = value.get("method")
    if not isinstance(method, str) or not method or len(method) > 128:
        raise ControlError("invalid_request", "Request method is invalid.")
    params = value.get("params", {})
    if not isinstance(params, dict):
        raise ControlError("invalid_params", "Request params must be an object.")
    return ControlRequest(request_id=request_id, method=method, params=params)


def response_envelope(request_id: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol": CONTROL_PROTOCOL,
        "version": CONTROL_VERSION,
        "type": "response",
        "id": request_id,
        "result": result,
    }


def error_envelope(request_id: str, error: ControlError) -> dict[str, Any]:
    return {
        "protocol": CONTROL_PROTOCOL,
        "version": CONTROL_VERSION,
        "type": "error",
        "id": request_id,
        "error": {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
        },
    }


def event_envelope(sequence: int, event: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol": CONTROL_PROTOCOL,
        "version": CONTROL_VERSION,
        "type": "event",
        "sequence": sequence,
        "event": event,
        "payload": payload,
    }
