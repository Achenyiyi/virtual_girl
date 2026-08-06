from __future__ import annotations

import asyncio
from enum import StrEnum

import pytest

from companion.audio.microphone import MicConfig
from companion.desktop.voice import DesktopVoiceController


class FakeState(StrEnum):
    ASR_PROCESSING = "asr_processing"
    LLM_THINKING = "llm_thinking"


class FakePipeline:
    def __init__(self, state: FakeState) -> None:
        self.active_turn_state = state


class FakeMicrophone:
    speech_start_sequence = 0

    def __init__(self) -> None:
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


async def _state_changed(_state: dict[str, object]) -> None:
    return None


@pytest.mark.asyncio
async def test_stop_cancels_voice_turn_before_generation() -> None:
    controller = DesktopVoiceController(
        MicConfig(), FakePipeline(FakeState.ASR_PROCESSING), _state_changed
    )
    microphone = FakeMicrophone()
    response_task = asyncio.create_task(asyncio.sleep(60))
    controller._enabled = True
    controller._microphone = microphone
    controller._response_task = response_task

    await controller.stop()

    assert microphone.stopped is True
    assert response_task.cancelled()


@pytest.mark.asyncio
async def test_stop_allows_voice_turn_in_generation_to_finish() -> None:
    controller = DesktopVoiceController(
        MicConfig(), FakePipeline(FakeState.LLM_THINKING), _state_changed
    )
    microphone = FakeMicrophone()
    release = asyncio.Event()
    response_task = asyncio.create_task(release.wait())
    controller._enabled = True
    controller._microphone = microphone
    controller._response_task = response_task

    await controller.stop()

    assert microphone.stopped is True
    assert response_task.cancelled() is False
    release.set()
    await response_task
