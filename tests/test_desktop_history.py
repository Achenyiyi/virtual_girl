from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from companion.desktop.history import ConversationHistoryProjector, HistoryCursorError


class LedgerMemory:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self.events = events

    async def query_events(self, query):
        rows = [event for event in self.events]
        if query.start_time:
            rows = [
                event
                for event in rows
                if event["occurred_at"] >= query.start_time.isoformat()
            ]
        if query.end_time:
            rows = [
                event
                for event in rows
                if event["occurred_at"] <= query.end_time.isoformat()
            ]
        if not query.sort_ascending:
            rows = list(reversed(rows))
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
async def test_history_pages_never_duplicate_or_drop_turns() -> None:
    projector = ConversationHistoryProjector(
        LedgerMemory(
            [_event(i, "session-a", f"问题{i}", f"回答{i}") for i in range(1, 6)]
        )
    )

    first = await projector.history("session-a", limit=2)
    assert [turn["turn_id"] for turn in first["turns"]] == ["turn-1", "turn-2"]
    assert first["next_cursor"]

    second = await projector.history("session-a", cursor=first["next_cursor"], limit=2)
    assert [turn["turn_id"] for turn in second["turns"]] == ["turn-3", "turn-4"]
    assert second["next_cursor"]

    third = await projector.history("session-a", cursor=second["next_cursor"], limit=2)
    assert [turn["turn_id"] for turn in third["turns"]] == ["turn-5"]
    assert third["next_cursor"] == ""


@pytest.mark.asyncio
async def test_history_pushdown_sends_lower_bound() -> None:
    seen_start_times: list[object] = []

    class RecordingMemory(LedgerMemory):
        async def query_events(self, query):
            seen_start_times.append(query.start_time)
            return await super().query_events(query)

    projector = ConversationHistoryProjector(
        RecordingMemory([_event(i, "session-a", "问题", "回答") for i in range(1, 6)])
    )

    first = await projector.history("session-a", limit=2)
    assert first["next_cursor"]
    await projector.history("session-a", cursor=first["next_cursor"], limit=2)

    assert seen_start_times[0] is None  # first page reads the whole ledger
    assert seen_start_times[1] is not None  # later pages prune already-read rows


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
