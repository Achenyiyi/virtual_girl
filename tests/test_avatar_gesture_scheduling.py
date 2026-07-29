"""Regression tests for post-conversation one-shot avatar gestures."""

from __future__ import annotations

import pytest

from companion.core.event_bus import EventBus
from companion.core.orchestrator import CompanionOrchestrator
from companion.core.policy_gate import PolicyGate
from companion.core.state_manager import StateManager
from companion.providers.avatar import AvatarState
from tests.test_providers import MockAvatarProvider, MockLLMProvider


class RecordingAvatarProvider(MockAvatarProvider):
    def __init__(self, *, fail_gestures: bool = False) -> None:
        self.states: list[AvatarState] = []
        self.gestures: list[tuple[str, float]] = []
        self._fail_gestures = fail_gestures

    async def update_state(self, state: AvatarState) -> None:
        self.states.append(state)

    async def trigger_gesture(self, gesture_id: str, intensity: float = 0.5) -> None:
        self.gestures.append((gesture_id, intensity))
        if self._fail_gestures:
            raise RuntimeError("unsupported gesture")


def _orchestrator(
    state: StateManager, avatar: RecordingAvatarProvider
) -> CompanionOrchestrator:
    return CompanionOrchestrator(
        state_manager=state,
        event_bus=EventBus("gesture-test"),
        policy_gate=PolicyGate(),
        llm_provider=MockLLMProvider(),
        avatar_provider=avatar,
    )


@pytest.mark.asyncio
async def test_completed_turn_triggers_one_gesture_with_cooldown_and_deduplication() -> None:
    state = StateManager()
    state.apply_affect_event(delta_valence=0.3, delta_arousal=0.2)
    state.apply_affect_event(delta_valence=0.1, delta_uncertainty=-0.1)
    avatar = RecordingAvatarProvider()
    orchestrator = _orchestrator(state, avatar)
    now = 100.0
    orchestrator._monotonic = lambda: now

    assert await orchestrator.startup()
    assert avatar.gestures == []

    await orchestrator.process_user_input("hello", turn_id="turn-1")
    assert avatar.gestures == [("wave", 0.6)]
    assert all(snapshot.pose.gesture_id is None for snapshot in avatar.states)

    await orchestrator.process_user_input("hello", turn_id="turn-2")
    assert avatar.gestures == [("wave", 0.6)]

    now += 9.0
    await orchestrator.process_user_input("hello", turn_id="turn-3")
    assert avatar.gestures == [("wave", 0.6), ("lean_forward", 0.5)]

    now += 31.0
    await orchestrator.process_user_input("hello", turn_id="turn-4")
    assert avatar.gestures == [
        ("wave", 0.6),
        ("lean_forward", 0.5),
        ("wave", 0.6),
    ]


@pytest.mark.asyncio
async def test_failed_gesture_is_isolated_and_quarantined() -> None:
    state = StateManager()
    state.apply_affect_event(delta_uncertainty=0.3)
    state.apply_affect_event(delta_uncertainty=0.1)
    avatar = RecordingAvatarProvider(fail_gestures=True)
    orchestrator = _orchestrator(state, avatar)
    now = 100.0
    orchestrator._monotonic = lambda: now

    assert await orchestrator.startup()
    first = await orchestrator.process_user_input("hello", turn_id="turn-1")

    assert first["response_text"] == "mock response"
    assert orchestrator.turn_count == 1
    assert avatar.gestures == [("look_away", 0.4)]

    now += 31.0
    second = await orchestrator.process_user_input("hello", turn_id="turn-2")

    assert second["response_text"] == "mock response"
    assert orchestrator.turn_count == 2
    assert avatar.gestures == [("look_away", 0.4)]

    third = await orchestrator.process_user_input(
        "hello", turn_id="turn-3", session_id="new-session"
    )

    assert third["response_text"] == "mock response"
    assert orchestrator.turn_count == 3
    assert avatar.gestures == [("look_away", 0.4), ("look_away", 0.4)]
    assert all(snapshot.pose.gesture_id is None for snapshot in avatar.states)
