"""Regression tests for terminal and ordered text conversation turns."""

from __future__ import annotations

import asyncio
import json

import pytest

from companion.core.event_bus import EventBus
from companion.core.orchestrator import CompanionOrchestrator
from companion.core.policy_gate import PolicyGate
from companion.core.state_manager import StateManager
from companion.events.base import BaseEvent
from companion.events.conversation import (
    ConversationTurnCompletedEvent,
    ConversationTurnFailedEvent,
)
from companion.memory.memory_service import MemoryService, MemoryServiceConfig
from companion.providers.memory import EventQuery
from companion.providers.model import LLMRequest, LLMResponse
from tests.test_providers import MockLLMProvider


def _orchestrator(bus: EventBus, llm: MockLLMProvider | None) -> CompanionOrchestrator:
    return CompanionOrchestrator(
        state_manager=StateManager(),
        event_bus=bus,
        policy_gate=PolicyGate(),
        llm_provider=llm,
    )


def _capture_events(bus: EventBus) -> list[BaseEvent]:
    events: list[BaseEvent] = []
    bus.on_any()(events.append)
    return events


class FailingLLM(MockLLMProvider):
    async def generate(self, request: LLMRequest) -> LLMResponse:
        raise TimeoutError("sensitive provider detail")


class BlockingLLM(MockLLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.entered.set()
        await self.release.wait()
        return await super().generate(request)


class OrderedLLM(MockLLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[LLMRequest] = []
        self.first_entered = asyncio.Event()
        self.release_first = asyncio.Event()

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            self.first_entered.set()
            await self.release_first.wait()
        return LLMResponse(
            text=f"reply-{len(self.requests)}",
            turn_id=request.turn_id,
            model_id="ordered-mock",
            model_provider="local",
        )


@pytest.mark.asyncio
async def test_missing_llm_records_terminal_failure() -> None:
    bus = EventBus("missing-llm")
    events = _capture_events(bus)

    result = await _orchestrator(bus, None).process_user_input(
        "hello", turn_id="turn_missing", session_id="session"
    )

    assert result["response_text"] == "[LLM provider not configured]"
    assert [event.event_type for event in events] == [
        "conversation.turn.started",
        "conversation.turn.failed",
    ]
    failed = events[-1]
    assert isinstance(failed, ConversationTurnFailedEvent)
    assert failed.stage == "configuration"
    assert failed.retryable is False


@pytest.mark.asyncio
async def test_generation_failure_records_sanitized_terminal_event() -> None:
    bus = EventBus("generation-failure")
    events = _capture_events(bus)
    orchestrator = _orchestrator(bus, FailingLLM())

    result = await orchestrator.process_user_input("secret", turn_id="turn_failed")

    assert result["model_id"] == "error"
    assert orchestrator.turn_count == 0
    assert [event.event_type for event in events] == [
        "conversation.turn.started",
        "conversation.turn.failed",
    ]
    failed = events[-1]
    assert isinstance(failed, ConversationTurnFailedEvent)
    assert failed.stage == "generation"
    assert failed.error_type == "TimeoutError"
    assert "sensitive" not in failed.model_dump_json()


@pytest.mark.asyncio
async def test_generation_failure_is_durable(tmp_path) -> None:
    memory = MemoryService(MemoryServiceConfig(db_path=str(tmp_path / "lifecycle.db")))
    bus = EventBus("durable-failure", persistence_handler=memory.append_domain_event)
    orchestrator = _orchestrator(bus, FailingLLM())
    try:
        await orchestrator.process_user_input("secret", turn_id="turn_durable")
        rows = await memory.query_events(
            EventQuery(
                event_types=[
                    "conversation.turn.started",
                    "conversation.turn.completed",
                    "conversation.turn.failed",
                ]
            )
        )
    finally:
        await bus.shutdown()
        await memory.shutdown()

    assert [row["event_type"] for row in rows] == [
        "conversation.turn.started",
        "conversation.turn.failed",
    ]
    payload = json.loads(rows[-1]["payload_json"])
    assert payload["turn_id"] == "turn_durable"
    assert payload["stage"] == "generation"
    assert payload["error_type"] == "TimeoutError"


@pytest.mark.asyncio
async def test_cancellation_records_failure_before_propagating() -> None:
    bus = EventBus("cancelled-turn")
    events = _capture_events(bus)
    llm = BlockingLLM()
    orchestrator = _orchestrator(bus, llm)
    task = asyncio.create_task(orchestrator.process_user_input("wait", turn_id="turn_cancel"))
    await llm.entered.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert orchestrator.turn_count == 0
    assert [event.event_type for event in events] == [
        "conversation.turn.started",
        "conversation.turn.failed",
    ]
    failed = events[-1]
    assert isinstance(failed, ConversationTurnFailedEvent)
    assert failed.stage == "cancellation"


@pytest.mark.asyncio
async def test_cancellation_during_started_commit_records_terminal_failure() -> None:
    persisted: list[BaseEvent] = []
    started_entered = asyncio.Event()
    release_started = asyncio.Event()

    async def persist(event: BaseEvent) -> None:
        if event.event_type == "conversation.turn.started":
            started_entered.set()
            await release_started.wait()
        persisted.append(event)

    bus = EventBus("started-cancellation", persistence_handler=persist)
    orchestrator = _orchestrator(bus, MockLLMProvider())
    task = asyncio.create_task(
        orchestrator.process_user_input("hello", turn_id="turn_started_cancel")
    )
    await started_entered.wait()

    task.cancel()
    release_started.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [event.event_type for event in persisted] == [
        "conversation.turn.started",
        "conversation.turn.failed",
    ]
    assert orchestrator.turn_count == 0


@pytest.mark.asyncio
async def test_concurrent_text_turns_use_committed_history_in_order() -> None:
    bus = EventBus("ordered-turns")
    events = _capture_events(bus)
    llm = OrderedLLM()
    orchestrator = _orchestrator(bus, llm)
    first = asyncio.create_task(orchestrator.process_user_input("first", turn_id="turn_1"))
    await llm.first_entered.wait()
    second = asyncio.create_task(orchestrator.process_user_input("second", turn_id="turn_2"))
    await asyncio.sleep(0)

    assert len(llm.requests) == 1
    llm.release_first.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result["response_text"] == "reply-1"
    assert second_result["response_text"] == "reply-2"
    assert llm.requests[1].messages[-3:] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply-1"},
        {"role": "user", "content": "second"},
    ]
    terminal = [
        event
        for event in events
        if isinstance(event, (ConversationTurnCompletedEvent, ConversationTurnFailedEvent))
    ]
    assert [event.turn_sequence for event in terminal] == [1, 2]
    assert [event.event_type for event in terminal] == [
        "conversation.turn.completed",
        "conversation.turn.completed",
    ]


@pytest.mark.asyncio
async def test_completed_persistence_failure_records_failed_terminal() -> None:
    persisted: list[BaseEvent] = []

    async def persist(event: BaseEvent) -> None:
        if event.event_type == "conversation.turn.completed":
            raise OSError("database unavailable")
        persisted.append(event)

    bus = EventBus("persistence-failure", persistence_handler=persist)
    orchestrator = _orchestrator(bus, MockLLMProvider())

    result = await orchestrator.process_user_input("hello", turn_id="turn_persistence")

    assert result["model_id"] == "error"
    assert orchestrator.turn_count == 0
    assert [event.event_type for event in persisted] == [
        "conversation.turn.started",
        "conversation.llm.response",
        "conversation.turn.failed",
    ]
    failed = persisted[-1]
    assert isinstance(failed, ConversationTurnFailedEvent)
    assert failed.stage == "persistence"
    assert failed.error_type == "OSError"


@pytest.mark.asyncio
async def test_cancellation_during_completed_commit_keeps_one_terminal_state() -> None:
    persisted: list[BaseEvent] = []
    completed_entered = asyncio.Event()
    release_completed = asyncio.Event()

    async def persist(event: BaseEvent) -> None:
        if event.event_type == "conversation.turn.completed":
            completed_entered.set()
            await release_completed.wait()
        persisted.append(event)

    bus = EventBus("completed-cancellation", persistence_handler=persist)
    orchestrator = _orchestrator(bus, MockLLMProvider())
    task = asyncio.create_task(
        orchestrator.process_user_input("hello", turn_id="turn_completed_cancel")
    )
    await completed_entered.wait()

    task.cancel()
    release_completed.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    terminal = [
        event
        for event in persisted
        if isinstance(event, (ConversationTurnCompletedEvent, ConversationTurnFailedEvent))
    ]
    assert [event.event_type for event in terminal] == ["conversation.turn.completed"]
    assert orchestrator.turn_count == 1
