"""Integration tests for the voice pipeline (Phase 1)."""

from __future__ import annotations

import asyncio
import time

import pytest

from companion.audio.player import PlaybackResult
from companion.core.event_bus import EventBus
from companion.core.orchestrator import CompanionOrchestrator
from companion.core.policy_gate import PolicyGate, PolicyGateConfig
from companion.core.state_manager import StateManager
from companion.events.base import BaseEvent
from companion.events.conversation import (
    ConversationTurnCompletedEvent,
    ConversationTurnFailedEvent,
)
from companion.memory.memory_service import MemoryService, MemoryServiceConfig
from companion.providers.asr import ASRBatchRequest, ASRBatchResult
from companion.providers.model import LLMRequest, LLMResponse, LLMStreamChunk
from companion.providers.tts import TTSChunk, TTSRequest, TTSTimingSegment
from companion.services.voice_pipeline import VoicePipeline, VoicePipelineConfig
from tests.test_providers import MockASRProvider, MockLLMProvider, MockTTSProvider


class FakeAudioOutput:
    def __init__(self, *, blocking: bool = False) -> None:
        self.blocking = blocking
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.play_calls = 0
        self.stop_calls = 0
        self.interrupted = False

    async def play(self, pcm_data: bytes, sample_rate: int) -> PlaybackResult:
        started_at_ms = int(time.time() * 1000)
        started_at_ns = time.perf_counter_ns()
        self.play_calls += 1
        self.started.set()
        if self.blocking:
            await self.release.wait()
        return PlaybackResult(
            played_duration_ms=0 if self.interrupted else 100,
            was_interrupted=self.interrupted,
            stream_generation=1,
            started_at_ms=started_at_ms,
            started_at_ns=started_at_ns,
        )

    async def stop(self) -> None:
        self.stop_calls += 1
        self.interrupted = True
        self.release.set()

    async def finish(self) -> None:
        return None


async def captured_events(bus: EventBus) -> list[BaseEvent]:
    events: list[BaseEvent] = []
    await bus.replay(events.append)
    return events


def terminal_events(events: list[BaseEvent]) -> list[BaseEvent]:
    return [
        event
        for event in events
        if event.event_type
        in {
            "conversation.turn.completed",
            "conversation.turn.failed",
        }
    ]


@pytest.fixture
def pipeline():
    """Create a test voice pipeline with mock providers."""
    state = StateManager()
    bus = EventBus("test")
    policy = PolicyGate()
    policy._quiet_hours_enabled = False

    config = VoicePipelineConfig(
        log_latency_breakdown=False,
    )

    return VoicePipeline(
        state=state,
        bus=bus,
        policy=policy,
        asr=MockASRProvider(),
        llm=MockLLMProvider(),
        tts=MockTTSProvider(),
        config=config,
    )


class TestVoicePipelineIntegration:
    """Test the full voice pipeline with mock providers."""

    @pytest.mark.asyncio
    async def test_start_session(self, pipeline):
        session_id = await pipeline.start_session()
        assert session_id
        assert pipeline.get_current_state() == "idle"

    @pytest.mark.asyncio
    async def test_text_input_path(self, pipeline):
        """Text input bypasses ASR and goes LLM → TTS."""
        await pipeline.start_session()
        response = await pipeline.process_text_input("你好")
        assert response is not None
        assert len(response) > 0

    @pytest.mark.asyncio
    async def test_completed_event_uses_first_audio_latency(self, pipeline):
        pipeline._audio_output = FakeAudioOutput()
        await pipeline.process_text_input("hello", speak=True)
        events = await captured_events(pipeline._bus)
        completed = next(
            event for event in events if isinstance(event, ConversationTurnCompletedEvent)
        )
        turn = pipeline._turn_mgr.get_current_turn()

        assert turn is not None
        assert turn.first_audio_played_at_ms > 0
        assert completed.total_latency_ms == turn.total_latency_ms

    @pytest.mark.asyncio
    async def test_configured_asr_language_reaches_provider(self):
        class CapturingASR(MockASRProvider):
            request = None

            async def transcribe_batch(self, request):
                self.request = request
                return await super().transcribe_batch(request)

        asr = CapturingASR()
        voice = VoicePipeline(
            state=StateManager(),
            bus=EventBus("language-test"),
            policy=PolicyGate(PolicyGateConfig(quiet_hours_enabled=False)),
            asr=asr,
            llm=MockLLMProvider(),
            tts=MockTTSProvider(),
            config=VoicePipelineConfig(language="en"),
        )

        await voice.process_audio_input(b"\x00\x00" * 160)

        assert asr.request is not None
        assert asr.request.language == "en"

    @pytest.mark.asyncio
    async def test_interrupt_current_turn(self, pipeline):
        """Barge-in should cancel active generation."""
        output = FakeAudioOutput(blocking=True)
        pipeline._audio_output = output
        task = asyncio.create_task(pipeline.process_text_input("讲一个很长很长的故事", speak=True))
        await output.started.wait()
        interrupted = await pipeline.interrupt()
        assert interrupted
        result = await task
        assert result
        assert output.stop_calls == 1
        assert pipeline.get_current_state() == "idle"

        events = await captured_events(pipeline._bus)
        assert sum(event.event_type == "conversation.turn.interrupted" for event in events) == 1
        terminal = terminal_events(events)
        assert [event.event_type for event in terminal] == ["conversation.turn.failed"]
        assert isinstance(terminal[0], ConversationTurnFailedEvent)
        assert terminal[0].stage == "cancellation"

    @pytest.mark.asyncio
    async def test_interrupted_turn_rejects_new_work_until_terminal_is_published(self):
        persisted: list[BaseEvent] = []
        interruption_entered = asyncio.Event()
        release_interruption = asyncio.Event()

        async def persist(event: BaseEvent) -> None:
            if event.event_type == "conversation.turn.interrupted":
                interruption_entered.set()
                await release_interruption.wait()
            persisted.append(event)

        output = FakeAudioOutput(blocking=True)
        voice = VoicePipeline(
            state=StateManager(),
            bus=EventBus("interrupt-ordering", persistence_handler=persist),
            policy=PolicyGate(),
            llm=MockLLMProvider(),
            tts=MockTTSProvider(),
            audio_output=output,
        )
        first = asyncio.create_task(voice.process_text_input("first", speak=True))
        await output.started.wait()
        interruption = asyncio.create_task(voice.interrupt())
        await interruption_entered.wait()
        second = asyncio.create_task(voice.process_text_input("second"))
        await asyncio.sleep(0)

        assert not second.done()
        assert len([e for e in persisted if e.event_type == "conversation.turn.started"]) == 1

        release_interruption.set()
        assert await interruption
        assert await first == "mock response"
        assert await second == "mock response"
        events = persisted
        starts = [e for e in events if e.event_type == "conversation.turn.started"]
        terminals = terminal_events(events)
        assert len(starts) == 2
        assert len(terminals) == 2
        assert terminals[0].event_type == "conversation.turn.failed"
        assert terminals[1].event_type == "conversation.turn.completed"

    @pytest.mark.asyncio
    async def test_interrupt_commits_only_confirmed_played_text_to_runtime_history(self):
        class AlignedTTS(MockTTSProvider):
            async def synthesize_stream(self, request: TTSRequest):
                yield TTSChunk(
                    audio_bytes=b"a" * 48_000,
                    turn_id=request.turn_id,
                    segment_index=0,
                    sample_rate=24000,
                    is_first=True,
                    is_final=True,
                    text=request.text,
                    duration_ms=1000,
                    alignment=(
                        TTSTimingSegment("第一句。", 0, 300),
                        TTSTimingSegment("第二句。", 300, 900),
                    ),
                )

        class PartialInterruptOutput(FakeAudioOutput):
            async def play(self, pcm_data: bytes, sample_rate: int) -> PlaybackResult:
                self.play_calls += 1
                self.started.set()
                await self.release.wait()
                return PlaybackResult(played_duration_ms=350, was_interrupted=True)

        bus = EventBus("interrupt-history")
        state = StateManager()
        policy = PolicyGate()
        runtime = CompanionOrchestrator(
            state,
            bus,
            policy,
            llm_provider=MockLLMProvider(),
        )
        output = PartialInterruptOutput()
        voice = VoicePipeline(
            state=state,
            bus=bus,
            policy=policy,
            tts=AlignedTTS(),
            audio_output=output,
            runtime=runtime,
        )
        task = asyncio.create_task(voice.process_text_input("hello", speak=True))
        await output.started.wait()

        assert await voice.interrupt()
        await task
        await asyncio.sleep(0)
        history = runtime.get_conversation_history().get_recent_turns()
        events = await captured_events(bus)
        interrupted = next(
            event
            for event in events
            if event.event_type == "conversation.turn.interrupted"
        )
        completed = next(
            event for event in events if isinstance(event, ConversationTurnCompletedEvent)
        )

        assert len(history) == 1
        assert history[0].user_text == "hello"
        assert history[0].companion_text == "第一句。"
        assert interrupted.interrupted_at_audio_ms >= 0
        assert completed.companion_text == "第一句。"
        assert completed.was_interrupted is True

    @pytest.mark.asyncio
    async def test_cancelled_interrupt_call_still_persists_interrupted_terminal(self):
        persisted: list[BaseEvent] = []
        interrupt_entered = asyncio.Event()
        release_interrupt = asyncio.Event()

        async def persist(event: BaseEvent) -> None:
            if event.event_type == "conversation.turn.interrupted":
                interrupt_entered.set()
                await release_interrupt.wait()
            persisted.append(event)

        output = FakeAudioOutput(blocking=True)
        bus = EventBus("cancelled-interrupt", persistence_handler=persist)
        voice = VoicePipeline(
            state=StateManager(),
            bus=bus,
            policy=PolicyGate(),
            llm=MockLLMProvider(),
            tts=MockTTSProvider(),
            audio_output=output,
        )
        response_task = asyncio.create_task(voice.process_text_input("hello", speak=True))
        await output.started.wait()
        interrupt_task = asyncio.create_task(voice.interrupt())
        await interrupt_entered.wait()

        interrupt_task.cancel()
        release_interrupt.set()
        with pytest.raises(asyncio.CancelledError):
            await interrupt_task
        assert await response_task == "mock response"
        terminal = terminal_events(persisted)

        assert [event.event_type for event in terminal] == ["conversation.turn.failed"]

    @pytest.mark.asyncio
    async def test_interruption_persistence_failure_records_failed_terminal(self):
        persisted: list[BaseEvent] = []

        async def persist(event: BaseEvent) -> None:
            if event.event_type == "conversation.turn.interrupted":
                raise OSError("database offline")
            persisted.append(event)

        output = FakeAudioOutput(blocking=True)
        bus = EventBus("interrupt-persistence-failure", persistence_handler=persist)
        voice = VoicePipeline(
            state=StateManager(),
            bus=bus,
            policy=PolicyGate(),
            llm=MockLLMProvider(),
            tts=MockTTSProvider(),
            audio_output=output,
        )
        response_task = asyncio.create_task(voice.process_text_input("hello", speak=True))
        await output.started.wait()

        assert await voice.interrupt()
        assert await response_task == "mock response"
        terminal = terminal_events(persisted)

        assert [event.event_type for event in terminal] == ["conversation.turn.failed"]
        failed = terminal[0]
        assert isinstance(failed, ConversationTurnFailedEvent)
        assert failed.stage == "persistence"
        assert voice._turn_mgr.get_current_turn().state == "error"

    @pytest.mark.asyncio
    async def test_hung_provider_cancellation_cannot_block_interrupt(self):
        class HangingCancelLLM(MockLLMProvider):
            async def cancel(self, turn_id: str) -> bool:
                await asyncio.Event().wait()
                return False

        output = FakeAudioOutput(blocking=True)
        bus = EventBus("hung-cancel")
        voice = VoicePipeline(
            state=StateManager(),
            bus=bus,
            policy=PolicyGate(),
            llm=HangingCancelLLM(),
            tts=MockTTSProvider(),
            audio_output=output,
            config=VoicePipelineConfig(interrupt_timeout_seconds=0.02),
        )
        response_task = asyncio.create_task(voice.process_text_input("hello", speak=True))
        await output.started.wait()

        assert await asyncio.wait_for(voice.interrupt(), timeout=0.5)
        assert await response_task == "mock response"
        terminal = terminal_events(await captured_events(bus))

        assert [event.event_type for event in terminal] == ["conversation.turn.failed"]

    @pytest.mark.asyncio
    async def test_cancellation_ignoring_provider_cannot_block_interrupt(self):
        release = asyncio.Event()

        class CancellationIgnoringLLM(MockLLMProvider):
            async def cancel(self, turn_id: str) -> bool:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await release.wait()
                return False

        output = FakeAudioOutput(blocking=True)
        voice = VoicePipeline(
            state=StateManager(),
            bus=EventBus("cancel-resistant-provider"),
            policy=PolicyGate(),
            llm=CancellationIgnoringLLM(),
            tts=MockTTSProvider(),
            audio_output=output,
            config=VoicePipelineConfig(interrupt_timeout_seconds=0.02),
        )
        response_task = asyncio.create_task(voice.process_text_input("hello", speak=True))
        await output.started.wait()

        try:
            assert await asyncio.wait_for(voice.interrupt(), timeout=0.5)
            assert await response_task == "mock response"
        finally:
            release.set()
            await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_shutdown_interrupts_active_turn_and_rejects_new_work(self):
        class HangingASR(MockASRProvider):
            def __init__(self) -> None:
                self.entered = asyncio.Event()

            async def transcribe_batch(self, request: ASRBatchRequest) -> ASRBatchResult:
                self.entered.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        bus = EventBus("voice-shutdown")
        asr = HangingASR()
        voice = VoicePipeline(
            state=StateManager(),
            bus=bus,
            policy=PolicyGate(),
            asr=asr,
            llm=MockLLMProvider(),
            config=VoicePipelineConfig(
                interrupt_timeout_seconds=0.02,
                cleanup_timeout_seconds=0.05,
            ),
        )
        turn_task = asyncio.create_task(voice.process_audio_input(b"audio"))
        await asr.entered.wait()

        await asyncio.wait_for(voice.shutdown(), timeout=0.5)
        with pytest.raises(asyncio.CancelledError):
            await turn_task
        terminal = terminal_events(await captured_events(bus))

        assert [event.event_type for event in terminal] == ["conversation.turn.failed"]
        with pytest.raises(RuntimeError, match="shut down"):
            await voice.process_text_input("after shutdown")
        await voice.shutdown()

    @pytest.mark.asyncio
    async def test_audio_input_completes_full_spoken_event_chain(self, pipeline):
        output = FakeAudioOutput()
        pipeline._audio_output = output

        response = await pipeline.process_audio_input(b"mock pcm input")

        assert response == "mock response"
        assert output.play_calls == 1
        assert pipeline.get_current_state() == "idle"
        event_types: list[str] = []
        await pipeline._bus.replay(lambda event: event_types.append(event.event_type))
        assert event_types == [
            "conversation.turn.started",
            "conversation.asr.finalized",
            "conversation.tts.synthesized",
            "conversation.audio.played",
            "conversation.llm.response",
            "conversation.turn.completed",
        ]

    @pytest.mark.asyncio
    async def test_spoken_turn_starts_tts_before_final_llm_chunk(self):
        release_final = asyncio.Event()

        class StreamingLLM(MockLLMProvider):
            async def generate_stream(self, request: LLMRequest):
                yield LLMStreamChunk(
                    text="第一句。",
                    turn_id=request.turn_id,
                    is_first=True,
                )
                await release_final.wait()
                yield LLMStreamChunk(
                    text="第二句。",
                    turn_id=request.turn_id,
                    is_final=True,
                    token_index=1,
                )

        class ReleasingTTS(MockTTSProvider):
            async def synthesize_stream(self, request: TTSRequest):
                yield TTSChunk(
                    audio_bytes=b"a" * 4800,
                    turn_id=request.turn_id,
                    segment_index=request.segment_index,
                    sample_rate=24000,
                    is_first=True,
                    is_final=True,
                    text=request.text,
                    duration_ms=100,
                )

        class ReleasingOutput(FakeAudioOutput):
            async def play(self, pcm_data: bytes, sample_rate: int) -> PlaybackResult:
                started_at_ms = int(time.time() * 1000)
                started_at_ns = time.perf_counter_ns()
                release_final.set()
                await asyncio.sleep(0.002)
                result = await super().play(pcm_data, sample_rate)
                return PlaybackResult(
                    played_duration_ms=result.played_duration_ms,
                    was_interrupted=result.was_interrupted,
                    stream_generation=result.stream_generation,
                    output_underflow=result.output_underflow,
                    started_at_ms=started_at_ms,
                    started_at_ns=started_at_ns,
                )

        voice = VoicePipeline(
            state=StateManager(),
            bus=EventBus("stream-before-final"),
            policy=PolicyGate(),
            llm=StreamingLLM(),
            tts=ReleasingTTS(),
            audio_output=ReleasingOutput(),
        )

        response = await asyncio.wait_for(
            voice.process_text_input("hello", speak=True), timeout=0.5
        )

        assert response == "第一句。第二句。"
        assert release_final.is_set()
        snapshot = voice.get_voice_acceptance_snapshot()
        turn = voice._turn_mgr.get_current_turn()
        assert turn is not None
        assert snapshot.incremental_playback, (
            turn.first_audio_played_at_ms,
            turn.llm_completed_at_ms,
            turn.first_audio_before_llm_completed,
        )
        assert snapshot.pcm_continuous

    @pytest.mark.asyncio
    async def test_streaming_prefix_rewrite_fails_without_completing_turn(self):
        class RewritingLLM(MockLLMProvider):
            async def generate_stream(self, request: LLMRequest):
                yield LLMStreamChunk(
                    text="先说一句。",
                    turn_id=request.turn_id,
                    is_first=True,
                )
                yield LLMStreamChunk(
                    text="\nanalysis: 不应朗读。\nfinal: 完全不同。",
                    turn_id=request.turn_id,
                    is_final=True,
                    token_index=1,
                )

        bus = EventBus("stream-prefix-rewrite")
        voice = VoicePipeline(
            state=StateManager(),
            bus=bus,
            policy=PolicyGate(),
            llm=RewritingLLM(),
            tts=MockTTSProvider(),
            audio_output=FakeAudioOutput(),
        )

        response = await voice.process_text_input("hello", speak=True)
        events = await captured_events(bus)
        terminal = terminal_events(events)

        assert response == "[LLM generation failed]"
        assert [event.event_type for event in terminal] == ["conversation.turn.failed"]
        assert isinstance(terminal[0], ConversationTurnFailedEvent)
        assert terminal[0].stage == "generation"
        assert terminal[0].error_type == "AssistantOutputSafetyError"
        assert not any(isinstance(event, ConversationTurnCompletedEvent) for event in events)

    @pytest.mark.asyncio
    async def test_streaming_llm_failure_is_not_spoken_or_completed(self):
        class FailingStreamingLLM(MockLLMProvider):
            async def generate_stream(self, request: LLMRequest):
                raise RuntimeError("private provider details")
                yield LLMStreamChunk(  # pragma: no cover
                    text="unreachable", turn_id=request.turn_id
                )

        output = FakeAudioOutput()
        bus = EventBus("stream-generation-failure")
        voice = VoicePipeline(
            state=StateManager(),
            bus=bus,
            policy=PolicyGate(),
            llm=FailingStreamingLLM(),
            tts=MockTTSProvider(),
            audio_output=output,
        )

        response = await voice.process_text_input("hello", speak=True)
        events = await captured_events(bus)
        terminal = terminal_events(events)

        assert response == "[LLM generation failed]"
        assert output.play_calls == 0
        assert [event.event_type for event in terminal] == ["conversation.turn.failed"]
        assert isinstance(terminal[0], ConversationTurnFailedEvent)
        assert terminal[0].stage == "generation"
        assert terminal[0].error_type == "RuntimeError"

    @pytest.mark.asyncio
    async def test_streaming_tts_failure_before_audio_is_reported_as_tts(self):
        class FailingStreamingTTS(MockTTSProvider):
            async def synthesize_stream(self, request: TTSRequest):
                raise RuntimeError("private provider details")
                yield TTSChunk(  # pragma: no cover
                    audio_bytes=b"unreachable", turn_id=request.turn_id
                )

        bus = EventBus("stream-tts-failure")
        voice = VoicePipeline(
            state=StateManager(),
            bus=bus,
            policy=PolicyGate(),
            llm=MockLLMProvider(),
            tts=FailingStreamingTTS(),
            audio_output=FakeAudioOutput(),
        )

        response = await voice.process_text_input("hello", speak=True)
        terminal = terminal_events(await captured_events(bus))

        assert response == "[Audio playback failed]"
        assert [event.event_type for event in terminal] == ["conversation.turn.failed"]
        assert isinstance(terminal[0], ConversationTurnFailedEvent)
        assert terminal[0].stage == "tts"
        assert terminal[0].error_type == "RuntimeError"

    @pytest.mark.asyncio
    async def test_timestamp_alignment_controls_completed_communicated_text(self):
        class AlignedTTS(MockTTSProvider):
            async def synthesize_stream(self, request: TTSRequest):
                yield TTSChunk(
                    audio_bytes=b"a" * 48_000,
                    turn_id=request.turn_id,
                    segment_index=request.segment_index,
                    sample_rate=24000,
                    is_first=True,
                    is_final=True,
                    text=request.text,
                    duration_ms=1000,
                    alignment=(
                        TTSTimingSegment("mock ", 0, 300),
                        TTSTimingSegment("response", 300, 900),
                    ),
                )

        class PartialOutput(FakeAudioOutput):
            async def play(self, pcm_data: bytes, sample_rate: int) -> PlaybackResult:
                self.play_calls += 1
                return PlaybackResult(played_duration_ms=350, was_interrupted=False)

        bus = EventBus("aligned-completion")
        voice = VoicePipeline(
            state=StateManager(),
            bus=bus,
            policy=PolicyGate(),
            llm=MockLLMProvider(),
            tts=AlignedTTS(),
            audio_output=PartialOutput(),
        )

        response = await voice.process_text_input("hello", speak=True)
        completed = next(
            event
            for event in await captured_events(bus)
            if isinstance(event, ConversationTurnCompletedEvent)
        )

        assert response == "mock response"
        assert completed.companion_text == "mock "
        assert completed.companion_full_text == "mock response"

    @pytest.mark.asyncio
    async def test_alignment_snapshots_remain_distinct_across_phrase_requests(self):
        class TwoPhraseLLM(MockLLMProvider):
            async def generate_stream(self, request: LLMRequest):
                yield LLMStreamChunk(text="第一句。", turn_id=request.turn_id, is_first=True)
                yield LLMStreamChunk(
                    text="第二句。",
                    turn_id=request.turn_id,
                    is_final=True,
                    token_index=1,
                )

        class PerPhraseAlignedTTS(MockTTSProvider):
            async def synthesize_stream(self, request: TTSRequest):
                yield TTSChunk(
                    audio_bytes=b"a" * 4800,
                    turn_id=request.turn_id,
                    segment_index=0,
                    sample_rate=24000,
                    is_first=True,
                    is_final=True,
                    text=request.text,
                    duration_ms=100,
                    alignment=(TTSTimingSegment(request.text, 0, 100, chunk_seq=0),),
                )

        bus = EventBus("phrase-alignment")
        voice = VoicePipeline(
            state=StateManager(),
            bus=bus,
            policy=PolicyGate(),
            llm=TwoPhraseLLM(),
            tts=PerPhraseAlignedTTS(),
            audio_output=FakeAudioOutput(),
        )

        response = await voice.process_text_input("hello", speak=True)
        completed = next(
            event
            for event in await captured_events(bus)
            if isinstance(event, ConversationTurnCompletedEvent)
        )

        assert response == "第一句。第二句。"
        assert completed.companion_text == response

    @pytest.mark.asyncio
    async def test_playback_failure_does_not_commit_turn(self, pipeline):
        class FailingOutput(FakeAudioOutput):
            async def play(self, pcm_data: bytes, sample_rate: int) -> PlaybackResult:
                raise RuntimeError("device disconnected")

        pipeline._audio_output = FailingOutput()
        response = await pipeline.process_text_input("你好", speak=True)
        assert response == "[Audio playback failed]"
        assert pipeline.get_current_state() == "idle"
        event_types: list[str] = []
        await pipeline._bus.replay(lambda event: event_types.append(event.event_type))
        assert "conversation.turn.completed" not in event_types
        assert "conversation.turn.failed" in event_types

    @pytest.mark.asyncio
    async def test_asr_failure_records_sanitized_terminal_event(self):
        class FailingASR(MockASRProvider):
            async def transcribe_batch(self, request: ASRBatchRequest) -> ASRBatchResult:
                raise TimeoutError("credential must not be persisted")

        bus = EventBus("asr-failure")
        voice = VoicePipeline(
            state=StateManager(),
            bus=bus,
            policy=PolicyGate(),
            asr=FailingASR(),
            llm=MockLLMProvider(),
        )

        response = await voice.process_audio_input(b"audio")
        terminal = terminal_events(await captured_events(bus))

        assert response == "[ASR transcription failed]"
        assert len(terminal) == 1
        failed = terminal[0]
        assert isinstance(failed, ConversationTurnFailedEvent)
        assert failed.stage == "asr"
        assert failed.error_type == "TimeoutError"
        assert "credential" not in failed.model_dump_json()

    @pytest.mark.asyncio
    async def test_llm_failure_records_terminal_event(self):
        class FailingLLM(MockLLMProvider):
            async def generate(self, request: LLMRequest) -> LLMResponse:
                raise ConnectionError("private endpoint")

        bus = EventBus("llm-failure")
        voice = VoicePipeline(
            state=StateManager(),
            bus=bus,
            policy=PolicyGate(),
            llm=FailingLLM(),
        )

        response = await voice.process_text_input("hello")
        terminal = terminal_events(await captured_events(bus))

        assert response == "[LLM generation failed]"
        assert len(terminal) == 1
        failed = terminal[0]
        assert isinstance(failed, ConversationTurnFailedEvent)
        assert failed.stage == "generation"
        assert failed.error_type == "ConnectionError"

    @pytest.mark.asyncio
    async def test_tts_failure_records_terminal_event(self):
        class FailingTTS(MockTTSProvider):
            async def synthesize_stream(self, request: TTSRequest):
                raise TimeoutError("provider token")
                yield  # pragma: no cover

        bus = EventBus("tts-failure")
        voice = VoicePipeline(
            state=StateManager(),
            bus=bus,
            policy=PolicyGate(),
            llm=MockLLMProvider(),
            tts=FailingTTS(),
            audio_output=FakeAudioOutput(),
        )

        response = await voice.process_text_input("hello", speak=True)
        terminal = terminal_events(await captured_events(bus))

        assert response == "[Audio playback failed]"
        assert len(terminal) == 1
        failed = terminal[0]
        assert isinstance(failed, ConversationTurnFailedEvent)
        assert failed.stage == "tts"
        assert failed.error_type == "TimeoutError"

    @pytest.mark.asyncio
    async def test_hung_tts_chunk_hits_hard_timeout(self):
        class HangingTTS(MockTTSProvider):
            async def synthesize_stream(self, request: TTSRequest):
                await asyncio.Event().wait()
                yield  # pragma: no cover

        bus = EventBus("hung-tts")
        voice = VoicePipeline(
            state=StateManager(),
            bus=bus,
            policy=PolicyGate(),
            llm=MockLLMProvider(),
            tts=HangingTTS(),
            audio_output=FakeAudioOutput(),
            config=VoicePipelineConfig(tts_chunk_timeout_seconds=0.02),
        )

        response = await asyncio.wait_for(
            voice.process_text_input("hello", speak=True), timeout=0.5
        )
        failed = terminal_events(await captured_events(bus))[0]

        assert response == "[Audio playback failed]"
        assert isinstance(failed, ConversationTurnFailedEvent)
        assert failed.stage == "tts"
        assert failed.error_type == "tts_chunk_timeout"

    @pytest.mark.asyncio
    async def test_cancellation_ignoring_tts_cannot_defeat_hard_timeout(self):
        release = asyncio.Event()

        class CancellationIgnoringTTS(MockTTSProvider):
            async def synthesize_stream(self, request: TTSRequest):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await release.wait()
                return
                yield  # pragma: no cover

        bus = EventBus("cancel-resistant-tts")
        voice = VoicePipeline(
            state=StateManager(),
            bus=bus,
            policy=PolicyGate(),
            llm=MockLLMProvider(),
            tts=CancellationIgnoringTTS(),
            audio_output=FakeAudioOutput(),
            config=VoicePipelineConfig(tts_chunk_timeout_seconds=0.02),
        )

        try:
            response = await asyncio.wait_for(
                voice.process_text_input("hello", speak=True), timeout=0.5
            )
            failed = terminal_events(await captured_events(bus))[0]

            assert response == "[Audio playback failed]"
            assert isinstance(failed, ConversationTurnFailedEvent)
            assert failed.stage == "tts"
            assert failed.error_type == "tts_chunk_timeout"
        finally:
            release.set()
            await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_cancellation_ignoring_llm_cannot_defeat_turn_timeout(self):
        release = asyncio.Event()

        class CancellationIgnoringLLM(MockLLMProvider):
            async def generate(self, request: LLMRequest) -> LLMResponse:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await release.wait()
                return await super().generate(request)

        bus = EventBus("cancel-resistant-llm")
        voice = VoicePipeline(
            state=StateManager(),
            bus=bus,
            policy=PolicyGate(),
            llm=CancellationIgnoringLLM(),
            config=VoicePipelineConfig(max_turn_duration_ms=20),
        )

        try:
            response = await asyncio.wait_for(
                voice.process_text_input("hello"), timeout=0.5
            )
            failed = terminal_events(await captured_events(bus))[0]

            assert response == "[Voice turn timed out]"
            assert isinstance(failed, ConversationTurnFailedEvent)
            assert failed.stage == "generation"
            assert failed.error_type == "turn_timeout"
        finally:
            release.set()
            await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_hung_playback_hits_hard_timeout(self):
        class HangingOutput(FakeAudioOutput):
            async def play(self, pcm_data: bytes, sample_rate: int) -> PlaybackResult:
                self.started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        bus = EventBus("hung-playback")
        voice = VoicePipeline(
            state=StateManager(),
            bus=bus,
            policy=PolicyGate(),
            llm=MockLLMProvider(),
            tts=MockTTSProvider(),
            audio_output=HangingOutput(),
            config=VoicePipelineConfig(playback_timeout_seconds=0.02),
        )

        response = await asyncio.wait_for(
            voice.process_text_input("hello", speak=True), timeout=0.5
        )
        failed = terminal_events(await captured_events(bus))[0]

        assert response == "[Audio playback failed]"
        assert isinstance(failed, ConversationTurnFailedEvent)
        assert failed.stage == "playback"
        assert failed.error_type == "playback_timeout"

    @pytest.mark.asyncio
    async def test_hung_playback_finish_hits_cleanup_timeout(self):
        class HangingFinishOutput(FakeAudioOutput):
            async def finish(self) -> None:
                await asyncio.Event().wait()

        bus = EventBus("hung-playback-finish")
        voice = VoicePipeline(
            state=StateManager(),
            bus=bus,
            policy=PolicyGate(),
            llm=MockLLMProvider(),
            tts=MockTTSProvider(),
            audio_output=HangingFinishOutput(),
            config=VoicePipelineConfig(cleanup_timeout_seconds=0.02),
        )

        response = await asyncio.wait_for(
            voice.process_text_input("hello", speak=True), timeout=0.5
        )
        failed = terminal_events(await captured_events(bus))[0]

        assert response == "[Audio playback failed]"
        assert isinstance(failed, ConversationTurnFailedEvent)
        assert failed.stage == "playback"
        assert failed.error_type == "playback_cleanup_timeout"

    @pytest.mark.asyncio
    async def test_whole_turn_budget_bounds_hung_asr(self):
        class HangingASR(MockASRProvider):
            async def transcribe_batch(self, request: ASRBatchRequest) -> ASRBatchResult:
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        bus = EventBus("whole-turn-timeout")
        voice = VoicePipeline(
            state=StateManager(),
            bus=bus,
            policy=PolicyGate(),
            asr=HangingASR(),
            llm=MockLLMProvider(),
            config=VoicePipelineConfig(max_turn_duration_ms=20),
        )

        response = await asyncio.wait_for(voice.process_audio_input(b"audio"), timeout=0.5)
        failed = terminal_events(await captured_events(bus))[0]

        assert response == "[Voice turn timed out]"
        assert isinstance(failed, ConversationTurnFailedEvent)
        assert failed.stage == "asr"
        assert failed.error_type == "turn_timeout"

    @pytest.mark.asyncio
    async def test_task_cancellation_records_terminal_failure(self):
        class BlockingLLM(MockLLMProvider):
            def __init__(self) -> None:
                super().__init__()
                self.entered = asyncio.Event()

            async def generate(self, request: LLMRequest) -> LLMResponse:
                self.entered.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        bus = EventBus("cancelled-voice")
        llm = BlockingLLM()
        voice = VoicePipeline(
            state=StateManager(),
            bus=bus,
            policy=PolicyGate(),
            llm=llm,
        )
        task = asyncio.create_task(voice.process_text_input("hello"))
        await llm.entered.wait()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        terminal = terminal_events(await captured_events(bus))

        assert len(terminal) == 1
        failed = terminal[0]
        assert isinstance(failed, ConversationTurnFailedEvent)
        assert failed.stage == "cancellation"

    @pytest.mark.asyncio
    async def test_interrupt_during_asr_stops_before_llm_generation(self):
        class BlockingASR(MockASRProvider):
            def __init__(self) -> None:
                self.entered = asyncio.Event()
                self.release = asyncio.Event()
                self.cancel_calls = 0

            async def transcribe_batch(self, request: ASRBatchRequest) -> ASRBatchResult:
                self.entered.set()
                await self.release.wait()
                return ASRBatchResult(text="late transcript")

            async def cancel(self, turn_id: str) -> bool:
                self.cancel_calls += 1
                self.release.set()
                return True

        class CountingLLM(MockLLMProvider):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            async def generate(self, request: LLMRequest) -> LLMResponse:
                self.calls += 1
                return await super().generate(request)

        bus = EventBus("asr-interrupt")
        asr = BlockingASR()
        llm = CountingLLM()
        voice = VoicePipeline(
            state=StateManager(),
            bus=bus,
            policy=PolicyGate(),
            asr=asr,
            llm=llm,
        )
        task = asyncio.create_task(voice.process_audio_input(b"audio"))
        await asr.entered.wait()

        assert await voice.interrupt()
        assert await task == ""
        terminal = terminal_events(await captured_events(bus))

        assert asr.cancel_calls == 1
        assert llm.calls == 0
        assert [event.event_type for event in terminal] == ["conversation.turn.failed"]

    @pytest.mark.asyncio
    async def test_interrupt_while_llm_event_commits_cannot_resume_playback(self):
        persisted: list[BaseEvent] = []
        llm_event_entered = asyncio.Event()
        release_llm_event = asyncio.Event()

        async def persist(event: BaseEvent) -> None:
            if event.event_type == "conversation.llm.response":
                llm_event_entered.set()
                await release_llm_event.wait()
            persisted.append(event)

        output = FakeAudioOutput()
        bus = EventBus("voice-llm-event-interrupt", persistence_handler=persist)
        voice = VoicePipeline(
            state=StateManager(),
            bus=bus,
            policy=PolicyGate(),
            llm=MockLLMProvider(),
            tts=MockTTSProvider(),
            audio_output=output,
        )
        task = asyncio.create_task(voice.process_text_input("hello", speak=True))
        await llm_event_entered.wait()

        interrupt_task = asyncio.create_task(voice.interrupt())
        release_llm_event.set()
        assert await interrupt_task
        assert await task == "mock response"
        terminal = terminal_events(persisted)

        assert output.play_calls == 1
        assert [event.event_type for event in terminal] == ["conversation.turn.completed"]
        assert isinstance(terminal[0], ConversationTurnCompletedEvent)
        assert terminal[0].was_interrupted is True

    @pytest.mark.asyncio
    async def test_interrupt_while_tts_event_commits_cannot_start_playback(self):
        persisted: list[BaseEvent] = []
        tts_event_entered = asyncio.Event()
        release_tts_event = asyncio.Event()

        async def persist(event: BaseEvent) -> None:
            if event.event_type == "conversation.tts.synthesized":
                tts_event_entered.set()
                await release_tts_event.wait()
            persisted.append(event)

        output = FakeAudioOutput()
        bus = EventBus("voice-tts-event-interrupt", persistence_handler=persist)
        voice = VoicePipeline(
            state=StateManager(),
            bus=bus,
            policy=PolicyGate(),
            llm=MockLLMProvider(),
            tts=MockTTSProvider(),
            audio_output=output,
        )
        task = asyncio.create_task(voice.process_text_input("hello", speak=True))
        await tts_event_entered.wait()

        interrupt_task = asyncio.create_task(voice.interrupt())
        release_tts_event.set()
        assert await interrupt_task
        assert await task == "mock response"
        terminal = terminal_events(persisted)

        assert output.play_calls == 0
        assert [event.event_type for event in terminal] == ["conversation.turn.failed"]

    @pytest.mark.asyncio
    async def test_concurrent_turns_are_serialized(self):
        class OrderedLLM(MockLLMProvider):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0
                self.first_entered = asyncio.Event()
                self.release_first = asyncio.Event()

            async def generate(self, request: LLMRequest) -> LLMResponse:
                self.calls += 1
                if self.calls == 1:
                    self.first_entered.set()
                    await self.release_first.wait()
                return await super().generate(request)

        bus = EventBus("serialized-voice")
        llm = OrderedLLM()
        voice = VoicePipeline(
            state=StateManager(),
            bus=bus,
            policy=PolicyGate(),
            llm=llm,
        )
        first = asyncio.create_task(voice.process_text_input("first"))
        await llm.first_entered.wait()
        second = asyncio.create_task(voice.process_text_input("second"))
        await asyncio.sleep(0)

        assert llm.calls == 1
        llm.release_first.set()
        assert await asyncio.gather(first, second) == ["mock response", "mock response"]
        terminal = terminal_events(await captured_events(bus))

        assert [event.event_type for event in terminal] == [
            "conversation.turn.completed",
            "conversation.turn.completed",
        ]
        assert [event.turn_sequence for event in terminal] == [1, 2]  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_completed_persistence_failure_records_failed_terminal(self):
        persisted: list[BaseEvent] = []

        async def persist(event: BaseEvent) -> None:
            if event.event_type == "conversation.turn.completed":
                raise OSError("database offline")
            persisted.append(event)

        bus = EventBus("voice-persistence-failure", persistence_handler=persist)
        voice = VoicePipeline(
            state=StateManager(),
            bus=bus,
            policy=PolicyGate(),
            llm=MockLLMProvider(),
        )

        response = await voice.process_text_input("hello")
        terminal = terminal_events(persisted)

        assert response == "[Conversation persistence failed]"
        assert [event.event_type for event in terminal] == ["conversation.turn.failed"]
        failed = terminal[0]
        assert isinstance(failed, ConversationTurnFailedEvent)
        assert failed.stage == "persistence"

    @pytest.mark.asyncio
    async def test_cancellation_during_completed_commit_keeps_completed_terminal(self):
        persisted: list[BaseEvent] = []
        completed_entered = asyncio.Event()
        release_completed = asyncio.Event()

        async def persist(event: BaseEvent) -> None:
            if event.event_type == "conversation.turn.completed":
                completed_entered.set()
                await release_completed.wait()
            persisted.append(event)

        bus = EventBus("voice-completed-cancellation", persistence_handler=persist)
        voice = VoicePipeline(
            state=StateManager(),
            bus=bus,
            policy=PolicyGate(),
            llm=MockLLMProvider(),
        )
        task = asyncio.create_task(voice.process_text_input("hello"))
        await completed_entered.wait()

        task.cancel()
        release_completed.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        terminal = terminal_events(persisted)

        assert [event.event_type for event in terminal] == ["conversation.turn.completed"]
        assert voice._turn_mgr.get_current_turn().state == "completed"

    @pytest.mark.asyncio
    async def test_interrupt_cannot_race_an_accepted_completion(self):
        persisted: list[BaseEvent] = []
        completed_entered = asyncio.Event()
        release_completed = asyncio.Event()

        async def persist(event: BaseEvent) -> None:
            if event.event_type == "conversation.turn.completed":
                completed_entered.set()
                await release_completed.wait()
            persisted.append(event)

        bus = EventBus("voice-completion-interrupt", persistence_handler=persist)
        voice = VoicePipeline(
            state=StateManager(),
            bus=bus,
            policy=PolicyGate(),
            llm=MockLLMProvider(),
        )
        task = asyncio.create_task(voice.process_text_input("hello"))
        await completed_entered.wait()

        assert await voice.interrupt() is False
        release_completed.set()
        assert await task == "mock response"
        terminal = terminal_events(persisted)

        assert [event.event_type for event in terminal] == ["conversation.turn.completed"]

    @pytest.mark.asyncio
    async def test_post_completion_history_failure_does_not_add_failed_terminal(self):
        class FailingHistoryRuntime(CompanionOrchestrator):
            async def commit_response(self, *args, **kwargs) -> None:
                raise RuntimeError("derived history unavailable")

        bus = EventBus("voice-history-degradation")
        runtime = FailingHistoryRuntime(
            StateManager(),
            bus,
            PolicyGate(),
            llm_provider=MockLLMProvider(),
        )
        voice = VoicePipeline(
            state=runtime.state,
            bus=bus,
            policy=runtime.policy,
            runtime=runtime,
        )

        response = await voice.process_text_input("hello")
        terminal = terminal_events(await captured_events(bus))

        assert response == "mock response"
        assert [event.event_type for event in terminal] == ["conversation.turn.completed"]

    @pytest.mark.asyncio
    async def test_spoken_turn_commits_to_runtime_memory(self, tmp_path):
        memory = MemoryService(MemoryServiceConfig(db_path=str(tmp_path / "voice_memory.db")))
        bus = EventBus("test", persistence_handler=memory.append_domain_event)
        state = StateManager()
        policy = PolicyGate()
        runtime = CompanionOrchestrator(
            state,
            bus,
            policy,
            llm_provider=MockLLMProvider(),
            memory_provider=memory,
        )
        pipeline = VoicePipeline(
            state=state,
            bus=bus,
            policy=policy,
            tts=MockTTSProvider(),
            audio_output=FakeAudioOutput(),
            runtime=runtime,
        )
        try:
            await pipeline.process_text_input("我喜欢蓝色", speak=True)
            facts = await memory.search_facts("蓝色")
            assert runtime.turn_count == 1
            assert len(facts) == 1
            assert (await memory.verify_consistency())["is_consistent"]
        finally:
            await memory.shutdown()

    @pytest.mark.asyncio
    async def test_latency_stats_collected(self, pipeline):
        """Pipeline should track latency metrics."""
        await pipeline.start_session()

        for i in range(3):
            await pipeline.process_text_input(f"消息{i}")

        stats = pipeline.get_latency_stats()
        assert stats["count"] == 3

    @pytest.mark.asyncio
    async def test_stop_session(self, pipeline):
        await pipeline.start_session()
        assert pipeline.get_current_state() == "idle"
        await pipeline.stop_session()
