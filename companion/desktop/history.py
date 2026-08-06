"""Read-only desktop conversation views projected from the event ledger."""

from __future__ import annotations

import base64
import json
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

from companion.providers.memory import EventQuery, MemoryProvider

_COMPLETED_EVENT = "conversation.turn.completed"
_BATCH_SIZE = 500
_MAX_PROJECTED_EVENTS = 20_000


class HistoryCursorError(ValueError):
    """A pagination cursor was malformed or belongs to another query."""


class ConversationHistoryProjector:
    """Project completed turns without creating a second chat database."""

    def __init__(self, memory: MemoryProvider) -> None:
        self._memory = memory

    async def list_sessions(
        self, *, cursor: str = "", limit: int = 20
    ) -> dict[str, Any]:
        bounded_limit = _bounded_limit(limit, default=20, maximum=50)
        cursor_data = _decode_cursor(cursor, expected_kind="sessions")
        snapshot = str(cursor_data.get("snapshot", ""))
        events = await self._completed_events(snapshot)
        if not snapshot:
            snapshot = _latest_occurred_at(events)
        visible = [event for event in events if str(event.get("occurred_at", "")) <= snapshot]
        sessions: OrderedDict[str, dict[str, Any]] = OrderedDict()
        for event in visible:
            payload = _payload(event)
            session_id = _safe_identifier(payload.get("session_id"))
            if not session_id:
                continue
            user_text = _safe_text(payload.get("user_text"))
            session = sessions.get(session_id)
            if session is None:
                session = {
                    "session_id": session_id,
                    "title": user_text[:28] or "未命名会话",
                    "updated_at": str(event.get("occurred_at", "")),
                    "turn_count": 0,
                    "preview": _safe_text(payload.get("companion_text"))[:96],
                }
                sessions[session_id] = session
            elif user_text:
                # Events are projected newest first. Replacing the title while walking
                # backwards leaves the earliest completed user text as the session title,
                # while insertion order and updated_at continue to reflect recent activity.
                session["title"] = user_text[:28]
            session["turn_count"] += 1
        rows = list(sessions.values())
        offset = _cursor_offset(cursor_data)
        page = rows[offset : offset + bounded_limit]
        next_offset = offset + len(page)
        return {
            "sessions": page,
            "next_cursor": (
                _encode_cursor("sessions", snapshot, next_offset)
                if next_offset < len(rows)
                else ""
            ),
        }

    async def history(
        self,
        session_id: str,
        *,
        cursor: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        if not session_id or len(session_id) > 128:
            raise ValueError("session_id is invalid")
        bounded_limit = _bounded_limit(limit, default=50, maximum=100)
        cursor_data = _decode_cursor(cursor, expected_kind=f"history:{session_id}")
        snapshot = str(cursor_data.get("snapshot", ""))
        events = await self._completed_events(snapshot)
        if not snapshot:
            snapshot = _latest_occurred_at(events)
        turns: list[dict[str, Any]] = []
        for event in reversed(events):
            if str(event.get("occurred_at", "")) > snapshot:
                continue
            payload = _payload(event)
            if payload.get("session_id") != session_id:
                continue
            turns.append(
                {
                    "event_id": _safe_identifier(event.get("event_id")),
                    "occurred_at": str(event.get("occurred_at", "")),
                    "turn_id": _safe_identifier(payload.get("turn_id")),
                    "turn_sequence": _safe_nonnegative_int(payload.get("turn_sequence")),
                    "user_text": _safe_text(payload.get("user_text")),
                    "companion_text": _safe_text(payload.get("companion_text")),
                    "was_interrupted": bool(payload.get("was_interrupted", False)),
                    "total_latency_ms": _safe_nonnegative_int(
                        payload.get("total_latency_ms")
                    ),
                    "language": _safe_text(payload.get("language")) or "zh",
                    "model_id": _safe_text(payload.get("model_id")),
                }
            )
        offset = _cursor_offset(cursor_data)
        page = turns[offset : offset + bounded_limit]
        next_offset = offset + len(page)
        return {
            "session_id": session_id,
            "turns": page,
            "next_cursor": (
                _encode_cursor(f"history:{session_id}", snapshot, next_offset)
                if next_offset < len(turns)
                else ""
            ),
        }

    async def _completed_events(self, snapshot: str = "") -> list[dict[str, Any]]:
        """Fetch completed-turn events, bounded by a cursor snapshot when given.

        The snapshot pushdown keeps paged history from re-reading the whole
        ledger; the round-trip guard skips it for formats the DB cannot compare.
        """
        end_time: datetime | None = None
        if snapshot:
            try:
                parsed = datetime.fromisoformat(snapshot)
                if str(parsed) == snapshot:
                    end_time = parsed
            except ValueError:
                pass
        events: list[dict[str, Any]] = []
        offset = 0
        while offset < _MAX_PROJECTED_EVENTS:
            batch = await self._memory.query_events(
                EventQuery(
                    event_types=[_COMPLETED_EVENT],
                    end_time=end_time,
                    limit=min(_BATCH_SIZE, _MAX_PROJECTED_EVENTS - offset),
                    offset=offset,
                    sort_ascending=False,
                )
            )
            events.extend(batch)
            if len(batch) < _BATCH_SIZE:
                break
            offset += len(batch)
        return events


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("payload_json", "{}")
    if not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _bounded_limit(value: int, *, default: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("limit must be an integer")
    if value == 0:
        return default
    if not 1 <= value <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return value


def _latest_occurred_at(events: list[dict[str, Any]]) -> str:
    if events:
        return str(events[0].get("occurred_at", ""))
    return datetime.now(UTC).isoformat()


def _encode_cursor(kind: str, snapshot: str, offset: int) -> str:
    raw = json.dumps(
        {"kind": kind, "snapshot": snapshot, "offset": offset},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, *, expected_kind: str) -> dict[str, Any]:
    if not cursor:
        return {}
    if len(cursor) > 1024:
        raise HistoryCursorError("cursor is invalid")
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoryCursorError("cursor is invalid") from exc
    if (
        not isinstance(value, dict)
        or value.get("kind") != expected_kind
        or not isinstance(value.get("snapshot"), str)
        or isinstance(value.get("offset"), bool)
        or not isinstance(value.get("offset"), int)
        or value["offset"] < 0
    ):
        raise HistoryCursorError("cursor is invalid")
    return value


def _cursor_offset(cursor: dict[str, Any]) -> int:
    return int(cursor.get("offset", 0))


def _safe_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _safe_identifier(value: object) -> str:
    text = _safe_text(value)
    if len(text) > 128 or any(ord(character) < 32 for character in text):
        return ""
    return text


def _safe_nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
