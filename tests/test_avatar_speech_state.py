"""Regression tests for lip-sync state pushed to the avatar bridge."""

from __future__ import annotations

import pytest

from companion.core.event_bus import EventBus
from companion.core.orchestrator import CompanionOrchestrator
from companion.core.policy_gate import PolicyGate
from companion.core.state_manager import StateManager
from companion.providers.avatar import AvatarState
from companion.services.voice_pipeline import _pcm_int16_rms
from tests.test_providers import MockLLMProvider


class RecordingAvatarProvider:
    """Minimal avatar provider that records state updates."""

    def __init__(self) -> None:
        self.states: list[AvatarState] = []

    async def update_state(self, state: AvatarState) -> None:
        self.states.append(state)

    async def set_proactive_level(self, level: int) -> None:
        return None


@pytest.mark.asyncio
async def test_set_avatar_speech_pushes_normalized_lip_state() -> None:
    avatar = RecordingAvatarProvider()
    orchestrator = CompanionOrchestrator(
        state_manager=StateManager(),
        event_bus=EventBus("avatar-speech"),
        policy_gate=PolicyGate(),
        llm_provider=MockLLMProvider(),
        avatar_provider=avatar,
    )

    await orchestrator.set_avatar_speech(
        speaking=True, mouth_open=1.7, audio_level=-0.2
    )

    assert avatar.states
    latest = avatar.states[-1]
    assert latest.is_speaking is True
    assert latest.expression.mouth_open == 1.0
    assert latest.audio_level == 0.0

    await orchestrator.set_avatar_speech(speaking=False)

    assert avatar.states[-1].is_speaking is False
    assert avatar.states[-1].expression.mouth_open == 0.0
    assert avatar.states[-1].audio_level == 0.0


def test_pcm_int16_rms_normalizes_signal() -> None:
    assert _pcm_int16_rms(b"") == 0.0
    assert _pcm_int16_rms(b"\x00\x00" * 100) == 0.0
    assert _pcm_int16_rms(b"\x00") == 0.0

    full_scale = b"\xff\x7f\x00\x80" * 100
    assert 0.99 <= _pcm_int16_rms(full_scale) <= 1.0
