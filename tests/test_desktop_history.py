from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from companion.desktop.history import ConversationHistoryProjector, HistoryCursorError


class LedgerMemory:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self.events = events

    async def query_events(self, query):
        rows = self.events if query.sort_ascending else list(reversed(self.events))
        return rows[query.offset : query.offset + query.limit]


def _event(
    index: int,
    session_id: str,
    user_text: str,
    companion_text: str,
) -> dict[str, object]:
    occurred_at = (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=index)).isoformat()
    return {
        "event_id": f"event-{index}",
        "event_type": "conversation.turn.completed",
        "occurred_at": occurred_at,
        "payload_json": json.dumps(
            {
                "session_id": session_id,
                "turn_id": f"turn-{index}",
                "turn_sequence": index,
                "user_text": user_text,
                "companion_text": companion_text,
                "companion_full_text": f"hidden-{index}",
                "was_interrupted": False,
                "total_latency_ms": index,
                "language": "zh",
                "model_id": "model",
            }
        ),
    }


@pytest.mark.asyncio
async def test_sessions_group_by_ledger_session_and_use_first_user_title() -> None:
    projector = ConversationHistoryProjector(
        LedgerMemory(
            [
                _event(1, "session-a", "第一条用户消息用于标题且超过二十八个字符的内容", "回答一"),
                _event(2, "session-a", "后续问题", "回答二"),
                _event(3, "session-b", "另一会话", "回答三"),
            ]
        )
    )

    result = await projector.list_sessions(limit=1)

    assert result["sessions"][0]["session_id"] == "session-b"
    assert result["sessions"][0]["title"] == "另一会话"
    assert result["next_cursor"]
    second = await projector.list_sessions(cursor=result["next_cursor"], limit=1)
    assert second["sessions"][0]["session_id"] == "session-a"
    assert second["sessions"][0]["turn_count"] == 2
    assert second["sessions"][0]["title"] == "第一条用户消息用于标题且超过二十八个字符的内容"[:28]


@pytest.mark.asyncio
async def test_history_is_stable_and_never_exposes_full_text() -> None:
    memory = LedgerMemory([_event(1, "session-a", "问题一", "回答一")])
    projector = ConversationHistoryProjector(memory)

    first = await projector.history("session-a", limit=1)
    memory.events.append(_event(2, "session-a", "问题二", "回答二"))
    stable = await projector.history(
        "session-a", cursor=first["next_cursor"], limit=1
    ) if first["next_cursor"] else first

    serialized = json.dumps(first, ensure_ascii=False)
    assert "companion_full_text" not in serialized
    assert "hidden-1" not in serialized
    assert stable["turns"][0]["companion_text"] == "回答一"


@pytest.mark.asyncio
async def test_cursor_cannot_cross_history_queries() -> None:
    projector = ConversationHistoryProjector(
        LedgerMemory([_event(1, "session-a", "问题", "回答")])
    )
    sessions = await projector.list_sessions(limit=1)
    if not sessions["next_cursor"]:
        # Produce a cursor through a second session.
        projector = ConversationHistoryProjector(
            LedgerMemory(
                [
                    _event(1, "session-a", "问题", "回答"),
                    _event(2, "session-b", "问题", "回答"),
                ]
            )
        )
        sessions = await projector.list_sessions(limit=1)

    with pytest.raises(HistoryCursorError):
        await projector.history("session-a", cursor=sessions["next_cursor"])
