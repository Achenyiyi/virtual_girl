"""Regression tests for the interactive production voice gate."""

from __future__ import annotations

import asyncio
import json
from argparse import Namespace
from types import SimpleNamespace

import pytest

from companion.__main__ import CompanionApp, async_main
from companion.core.event_bus import EventBus
from companion.events.conversation import (
    AsrFinalizedEvent,
    AudioPlayedEvent,
    ConversationTurnCompletedEvent,
    ConversationTurnFailedEvent,
    ConversationTurnInterruptedEvent,
    ConversationTurnStartedEvent,
    LlmResponseGeneratedEvent,
    TtsSynthesizedEvent,
)
from companion.voice_acceptance import run_voice_acceptance


class SequencedMicrophone:
    def __init__(self, utterances: list[bytes | None]) -> None:
        self._utterances = iter(utterances)
        self._speech_start_sequence = 0

    @property
    def speech_start_sequence(self) -> int:
        return self._speech_start_sequence

    async def get_speech_audio(self, timeout: float = 15.0) -> bytes | None:
        del timeout
        utterance = next(self._utterances)
        self._speech_start_sequence += 1
        return utterance

    async def wait_for_speech_start(self, after_sequence: int, timeout: float) -> int | None:
        del timeout
        while self._speech_start_sequence <= after_sequence:
            await asyncio.sleep(0)
        return self._speech_start_sequence


class AcceptancePipeline:
    def __init__(self, bus: EventBus, *, interrupt_delay: float = 0.0) -> None:
        self._bus = bus
        self._state = "idle"
        self._turn = 0
        self._release = asyncio.Event()
        self._interrupt_delay = interrupt_delay
        self._interrupted = False

    def get_current_state(self) -> str:
        return self._state

    def get_voice_acceptance_snapshot(self):
        terminal_state = "interrupted" if self._interrupted else "completed"
        return SimpleNamespace(
            terminal_state=terminal_state,
            incremental_playback=True,
            pcm_continuous=True,
            played_segment_count=2,
            output_underflow_count=0,
            history_matches_played_text=True,
        )

    async def process_audio_input(self, audio_bytes: bytes) -> str:
        assert audio_bytes
        self._turn += 1
        turn_id = f"acceptance-turn-{self._turn}"
        await self._bus.publish(
            ConversationTurnStartedEvent(
                session_id="acceptance-session",
                turn_id=turn_id,
                turn_sequence=self._turn,
                input_modality="voice",
            )
        )
        await self._bus.publish(
            AsrFinalizedEvent(
                turn_id=turn_id,
                segment_index=0,
                transcript="accepted speech",
                language="en",
                confidence=1.0,
                latency_ms=10,
            )
        )
        await self._bus.publish(
            LlmResponseGeneratedEvent(
                turn_id=turn_id,
                response_text="accepted response",
                model_id="test-model",
                model_provider="test",
                time_to_first_token_ms=10,
                total_latency_ms=20,
                token_count=2,
            )
        )
        await self._bus.publish(
            TtsSynthesizedEvent(
                turn_id=turn_id,
                segment_index=0,
                text="accepted response",
                audio_duration_ms=100,
                time_to_first_byte_ms=20,
                tts_provider="test",
            )
        )
        await self._bus.publish(
            AudioPlayedEvent(
                turn_id=turn_id,
                segment_index=0,
                audio_hash="0" * 64,
                played_duration_ms=100,
            )
        )
        if self._turn == 1:
            await self._bus.publish(
                ConversationTurnCompletedEvent(
                    turn_id=turn_id,
                    session_id="acceptance-session",
                    turn_sequence=self._turn,
                    user_text="accepted speech",
                    companion_text="accepted response",
                    companion_full_text="accepted response",
                    total_latency_ms=400,
                    model_id="test-model",
                )
            )
            return "accepted response"

        self._state = "speaking"
        await self._release.wait()
        self._state = "idle"
        return "interrupted response"

    async def interrupt(self) -> bool:
        await asyncio.sleep(self._interrupt_delay)
        self._interrupted = True
        await self._bus.publish(
            ConversationTurnInterruptedEvent(
                turn_id="acceptance-turn-2",
                interrupted_at_audio_ms=0,
                new_turn_id="pending",
                reason="user_speech",
            )
        )
        self._release.set()
        return True


@pytest.mark.asyncio
async def test_voice_acceptance_proves_complete_and_interrupted_paths() -> None:
    bus = EventBus("voice-acceptance")
    pipeline = AcceptancePipeline(bus)
    microphone = SequencedMicrophone([b"a" * 3200, b"b" * 3200, b"c" * 3200])

    report = await run_voice_acceptance(
        microphone=microphone,
        pipeline=pipeline,
        event_bus=bus,
        sample_rate=16_000,
        utterance_timeout_seconds=1.0,
        turn_timeout_seconds=1.0,
        target_e2e_latency_ms=900,
        target_interrupt_latency_ms=300,
        barge_in_guard_seconds=0.0,
    )

    assert report.exit_code == 0
    assert [check.code for check in report.checks] == [
        "voice.complete_turn",
        "voice.first_audio_latency",
        "voice.incremental_playback",
        "voice.pcm_continuity",
        "voice.completed_history",
        "voice.interrupt_terminal",
        "voice.interrupt_latency",
        "voice.interrupted_history",
    ]
    payload = json.loads(report.to_json())
    assert payload["passed"] is True
    assert payload["schema_version"] == 1
    assert payload["app_version"]
    assert payload["generated_at"]
    assert bus.subscriber_count == 0


@pytest.mark.asyncio
async def test_voice_acceptance_reports_sanitized_stage_failure() -> None:
    bus = EventBus("voice-acceptance-failure")

    class FailingPipeline:
        async def process_audio_input(self, audio_bytes: bytes) -> str:
            assert audio_bytes
            await bus.publish(
                ConversationTurnStartedEvent(
                    session_id="failure-session",
                    turn_id="failure-turn",
                    turn_sequence=1,
                    input_modality="voice",
                )
            )
            await bus.publish(
                ConversationTurnFailedEvent(
                    turn_id="failure-turn",
                    session_id="failure-session",
                    turn_sequence=1,
                    stage="tts",
                    error_type="CloudTTSError",
                    retryable=True,
                    elapsed_ms=20,
                )
            )
            return "[Audio playback failed]"

        async def interrupt(self) -> bool:
            return False

        def get_current_state(self) -> str:
            return "idle"

        def get_voice_acceptance_snapshot(self):
            return SimpleNamespace(
                terminal_state="error",
                incremental_playback=False,
                pcm_continuous=False,
                played_segment_count=0,
                output_underflow_count=0,
                history_matches_played_text=False,
            )

    report = await run_voice_acceptance(
        microphone=SequencedMicrophone([b"a" * 3200]),
        pipeline=FailingPipeline(),
        event_bus=bus,
        sample_rate=16_000,
        utterance_timeout_seconds=1.0,
        turn_timeout_seconds=1.0,
        target_e2e_latency_ms=900,
        target_interrupt_latency_ms=300,
        barge_in_guard_seconds=0.0,
    )

    assert report.exit_code == 1
    assert report.checks[0].message == "Voice stage failed: tts/CloudTTSError."


@pytest.mark.asyncio
async def test_voice_acceptance_fails_interrupt_latency_target() -> None:
    bus = EventBus("voice-acceptance-latency")
    pipeline = AcceptancePipeline(bus, interrupt_delay=0.03)

    report = await run_voice_acceptance(
        microphone=SequencedMicrophone([b"a" * 3200, b"b" * 3200, b"c" * 3200]),
        pipeline=pipeline,
        event_bus=bus,
        sample_rate=16_000,
        utterance_timeout_seconds=1.0,
        turn_timeout_seconds=1.0,
        target_e2e_latency_ms=900,
        target_interrupt_latency_ms=5,
        barge_in_guard_seconds=0.0,
    )

    assert report.exit_code == 1
    latency = next(check for check in report.checks if check.code == "voice.interrupt_latency")
    assert latency.actual_ms is not None and latency.actual_ms >= 5
    assert latency.target_ms == 5


@pytest.mark.asyncio
async def test_voice_acceptance_timeout_does_not_wait_for_cancellation_resistance() -> None:
    release = asyncio.Event()

    class StuckPipeline:
        async def process_audio_input(self, audio_bytes: bytes) -> str:
            assert audio_bytes
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            return "late"

        async def interrupt(self) -> bool:
            return False

        def get_current_state(self) -> str:
            return "processing"

    try:
        report = await asyncio.wait_for(
            run_voice_acceptance(
                microphone=SequencedMicrophone([b"a" * 3200]),
                pipeline=StuckPipeline(),
                event_bus=EventBus("voice-acceptance-stuck"),
                sample_rate=16_000,
                utterance_timeout_seconds=0.01,
                turn_timeout_seconds=0.01,
                target_e2e_latency_ms=900,
                target_interrupt_latency_ms=300,
                barge_in_guard_seconds=0.0,
            ),
            timeout=0.5,
        )
        assert report.exit_code == 1
        assert report.checks[0].message.endswith("TimeoutError.")
    finally:
        release.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_voice_acceptance_rejects_premature_echo_signal() -> None:
    bus = EventBus("voice-acceptance-echo")
    report = await run_voice_acceptance(
        microphone=SequencedMicrophone([b"a" * 3200, b"b" * 3200, b"echo" * 800]),
        pipeline=AcceptancePipeline(bus),
        event_bus=bus,
        sample_rate=16_000,
        utterance_timeout_seconds=1.0,
        turn_timeout_seconds=1.0,
        target_e2e_latency_ms=900,
        target_interrupt_latency_ms=300,
        barge_in_guard_seconds=0.05,
    )

    assert report.exit_code == 1
    assert report.checks[-1].code == "voice.echo_guard"


@pytest.mark.asyncio
async def test_voice_acceptance_json_reports_startup_failure(
    monkeypatch,
    tmp_path,
) -> None:
    original_stop = CompanionApp.stop

    async def not_ready(_self: CompanionApp) -> bool:
        return False

    async def stopped(_self: CompanionApp) -> None:
        await original_stop(_self)

    original_init = CompanionApp.__init__

    def noisy_init(self: CompanionApp, config) -> None:
        print("constructor noise that must not reach JSON stdout")
        original_init(self, config)

    monkeypatch.setattr(CompanionApp, "__init__", noisy_init)
    monkeypatch.setattr(CompanionApp, "start", not_ready)
    monkeypatch.setattr(CompanionApp, "stop", stopped)
    printed: list[str] = []
    monkeypatch.setattr("builtins.print", lambda value, **_kwargs: printed.append(str(value)))
    config_path = tmp_path / "voice-acceptance.yaml"
    config_path.write_text(
        "runtime:\n  data_root: config_directory\n",
        encoding="utf-8",
    )
    args = Namespace(
        config=config_path,
        doctor=False,
        doctor_online=False,
        doctor_json=False,
        doctor_voice_hardware=False,
        accept_voice=False,
        accept_voice_json=True,
        backup_memory=None,
        verify_memory_backup=None,
        overwrite_backup=False,
        log_level=None,
        voice_input=False,
        voice=False,
        once=None,
    )

    assert await async_main(args) == 1
    payload = json.loads(printed[-1])
    assert payload["passed"] is False
    assert payload["checks"][0]["code"] == "voice.runtime_ready"
