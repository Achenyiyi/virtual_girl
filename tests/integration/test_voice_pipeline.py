"""Integration tests for the voice pipeline (Phase 1)."""

from __future__ import annotations

import asyncio

import pytest

from companion.audio.player import PlaybackResult
from companion.core.event_bus import EventBus
from companion.core.orchestrator import CompanionOrchestrator
from companion.core.policy_gate import PolicyGate, PolicyGateConfig
from companion.core.state_manager import StateManager
from companion.memory.memory_service import MemoryService, MemoryServiceConfig
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
        self.play_calls += 1
        self.started.set()
        if self.blocking:
            await self.release.wait()
        return PlaybackResult(
            played_duration_ms=0 if self.interrupted else 100,
            was_interrupted=self.interrupted,
        )

    async def stop(self) -> None:
        self.stop_calls += 1
        self.interrupted = True
        self.release.set()

    async def finish(self) -> None:
        return None


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
        assert pipeline.get_current_state() == "interrupted"

        event_types: list[str] = []
        await pipeline._bus.replay(lambda event: event_types.append(event.event_type))
        assert "conversation.turn.interrupted" in event_types
        assert "conversation.turn.completed" not in event_types

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
            "conversation.llm.response",
            "conversation.tts.synthesized",
            "conversation.audio.played",
            "conversation.turn.completed",
        ]

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
