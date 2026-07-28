"""Voice Pipeline — the real-time ASR → LLM → TTS coordination service.

This is the core of Phase 1. It coordinates:
1. Audio capture (microphone)
2. Voice Activity Detection (VAD)
3. Speech-to-Text (ASR)
4. Language Model (LLM)
5. Text-to-Speech (TTS)
6. Audio playback

Key features:
- Streaming pipeline with cancellation at any point
- Pre-roll buffer (300-500ms before VAD trigger, per AIRI #2092)
- Barge-in: user can interrupt at any time
- Turn management with turn_id tracking
- Only played audio enters shared history
- Latency tracking at every stage
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from companion.audio.player import PlaybackResult
from companion.core.event_bus import EventBus
from companion.core.orchestrator import CompanionOrchestrator
from companion.core.policy_gate import PolicyGate
from companion.core.state_manager import StateManager
from companion.events.base import generate_ulid
from companion.events.conversation import (
    AsrFinalizedEvent,
    AudioPlayedEvent,
    ConversationTurnCompletedEvent,
    ConversationTurnInterruptedEvent,
    ConversationTurnStartedEvent,
    LlmResponseGeneratedEvent,
    TtsSynthesizedEvent,
)
from companion.protocols.audio import AudioConfirmationProtocol
from companion.protocols.turn import TurnManager, TurnRecord, TurnState
from companion.providers.asr import ASRBatchRequest, ASRProvider
from companion.providers.model import LLMProvider, LLMRequest
from companion.providers.tts import TTSProvider, TTSRequest

logger = logging.getLogger(__name__)


class AudioOutput(Protocol):
    def play(self, pcm_data: bytes, sample_rate: int) -> Awaitable[PlaybackResult]: ...

    def stop(self) -> Awaitable[None]: ...

    def finish(self) -> Awaitable[None]: ...


class PipelineState(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"


@dataclass
class VoicePipelineConfig:
    """Configuration for the voice pipeline."""

    sample_rate: int = 16000
    language: str = "zh"
    pre_roll_ms: int = 400  # Pre-roll buffer before VAD trigger
    max_turn_duration_ms: int = 30_000  # Max turn before timeout
    echo_cancellation: bool = True
    noise_suppression: bool = True
    auto_gain_control: bool = True

    # Latency targets
    target_e2e_latency_ms: int = 900  # p50 target
    target_interrupt_latency_ms: int = 300  # p95 target

    # Debug
    log_latency_breakdown: bool = True

    def __post_init__(self) -> None:
        if self.sample_rate not in {8000, 16000, 24000, 48000}:
            raise ValueError("voice sample_rate is unsupported")
        if self.language not in {"zh", "en", "auto"}:
            raise ValueError("voice language must be zh, en, or auto")
        if not 0 <= self.pre_roll_ms <= 2000:
            raise ValueError("voice pre_roll_ms must be between 0 and 2000")
        if self.max_turn_duration_ms <= 0:
            raise ValueError("max_turn_duration_ms must be positive")


@dataclass
class PipelineMetrics:
    """Latency and quality metrics for a single turn."""

    turn_id: str = ""
    vad_latency_ms: int = 0
    asr_latency_ms: int = 0
    llm_ttft_ms: int = 0  # Time to first token
    llm_total_ms: int = 0
    tts_ttfb_ms: int = 0  # Time to first byte
    e2e_latency_ms: int = 0  # VAD trigger → first audio played
    interrupted: bool = False
    interruption_response_ms: int = 0


class VoicePipeline:
    """Real-time voice conversation pipeline.

    Example usage:
        pipeline = VoicePipeline(
            state, bus, policy, asr=asr_provider,
            llm=llm_provider, tts=tts_provider,
        )
        async for audio_chunk in pipeline.run():
            play_audio(audio_chunk)
    """

    def __init__(
        self,
        state: StateManager,
        bus: EventBus,
        policy: PolicyGate,
        *,
        asr: ASRProvider | None = None,
        llm: LLMProvider | None = None,
        tts: TTSProvider | None = None,
        audio_output: AudioOutput | None = None,
        runtime: CompanionOrchestrator | None = None,
        config: VoicePipelineConfig | None = None,
    ) -> None:
        self._state_mgr = state
        self._bus = bus
        self._policy = policy
        self._asr = asr
        self._llm = llm
        self._tts = tts
        self._audio_output = audio_output
        self._runtime = runtime
        self._config = config or VoicePipelineConfig()

        self._turn_mgr = TurnManager()
        self._audio_proto = AudioConfirmationProtocol()
        self._is_running = False
        self._pipeline_state = PipelineState.IDLE
        self._current_session_id: str = ""
        self._turn_sequence = 0

        # Audio input queue (filled by microphone, consumed by pipeline)
        self._audio_input: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)

        # Metrics collection
        self._metrics: list[PipelineMetrics] = []

    # ── Public API ────────────────────────────────────────────────────

    async def start_session(self, session_id: str | None = None) -> str:
        """Start a new conversation session."""
        self._current_session_id = session_id or f"sess_{generate_ulid()}"
        self._is_running = True
        self._pipeline_state = PipelineState.IDLE
        logger.info("Voice pipeline session started: %s", self._current_session_id)
        return self._current_session_id

    async def stop_session(self) -> None:
        """Stop the current session."""
        self._is_running = False
        self._pipeline_state = PipelineState.IDLE
        logger.info("Voice pipeline session stopped: %s", self._current_session_id)

    async def feed_audio(self, audio_bytes: bytes) -> None:
        """Feed raw audio data from the microphone into the pipeline."""
        if not self._is_running:
            return
        try:
            self._audio_input.put_nowait(audio_bytes)
        except asyncio.QueueFull:
            logger.warning("Audio input queue full, dropping frame")
            # Drop oldest to make room
            try:
                self._audio_input.get_nowait()
                self._audio_input.put_nowait(audio_bytes)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    async def process_text_input(self, text: str, *, speak: bool = False) -> str:
        """Process a text input through the pipeline (non-voice path).

        Returns the companion's text response.
        This bypasses ASR and goes directly to LLM → TTS path.
        """
        if not self._is_running:
            await self.start_session()

        turn = await self._begin_turn("text")
        self._turn_mgr.record_user_text(turn.turn_id, text)
        return await self._respond(turn, text, speak=speak)

    async def process_audio_input(self, audio_bytes: bytes) -> str:
        """Transcribe a completed utterance and run a spoken response turn."""
        if not self._is_running:
            await self.start_session()
        turn = await self._begin_turn("voice")
        if not self._asr:
            self._turn_mgr.transition(turn.turn_id, TurnState.ERROR)
            self._pipeline_state = PipelineState.IDLE
            return "[ASR provider not configured]"

        self._turn_mgr.transition(turn.turn_id, TurnState.ASR_PROCESSING)
        started_at = time.monotonic()
        try:
            transcript = await asyncio.wait_for(
                self._asr.transcribe_batch(
                    ASRBatchRequest(
                        audio_bytes=audio_bytes,
                        sample_rate=self._config.sample_rate,
                        language=self._config.language,
                        turn_id=turn.turn_id,
                    )
                ),
                timeout=self._config.max_turn_duration_ms / 1000,
            )
        except Exception:
            logger.exception("ASR transcription failed")
            self._turn_mgr.transition(turn.turn_id, TurnState.ERROR)
            self._pipeline_state = PipelineState.IDLE
            return "[ASR transcription failed]"

        asr_latency_ms = int((time.monotonic() - started_at) * 1000)
        if not transcript.text.strip():
            self._turn_mgr.transition(turn.turn_id, TurnState.ERROR)
            self._pipeline_state = PipelineState.IDLE
            return "[No speech recognized]"
        self._turn_mgr.record_user_text(turn.turn_id, transcript.text)
        await self._bus.publish(
            AsrFinalizedEvent(
                turn_id=turn.turn_id,
                segment_index=0,
                transcript=transcript.text,
                language=transcript.language,
                confidence=transcript.confidence,
                latency_ms=asr_latency_ms,
            )
        )
        return await self._respond(turn, transcript.text, speak=True)

    async def _begin_turn(self, modality: str) -> TurnRecord:
        self._turn_sequence += 1
        turn = self._turn_mgr.create_turn(self._current_session_id, self._turn_sequence)
        self._pipeline_state = PipelineState.PROCESSING
        await self._bus.publish(
            ConversationTurnStartedEvent(
                session_id=self._current_session_id,
                turn_id=turn.turn_id,
                turn_sequence=self._turn_sequence,
                input_modality=modality,
            )
        )
        return turn

    async def _respond(self, turn: TurnRecord, text: str, *, speak: bool) -> str:
        """Generate and optionally speak one already-started turn."""

        if not self._llm and not self._runtime:
            self._turn_mgr.transition(turn.turn_id, TurnState.ERROR)
            self._pipeline_state = PipelineState.IDLE
            return "[LLM provider not configured]"

        self._turn_mgr.transition(turn.turn_id, TurnState.LLM_THINKING)

        t0 = time.time()
        try:
            llm_request = LLMRequest(
                messages=[{"role": "user", "content": text}],
                system_prompt=self._state_mgr.get_system_prompt_fragment(),
                turn_id=turn.turn_id,
                max_tokens=512,
            )
            if self._runtime:
                response_task = self._runtime.prepare_response(text, turn.turn_id)
            else:
                llm = self._llm
                if llm is None:
                    raise RuntimeError("LLM provider not configured")
                response_task = llm.generate(llm_request)
            response = await asyncio.wait_for(
                response_task, timeout=self._config.max_turn_duration_ms / 1000
            )
        except Exception:
            logger.exception("LLM generation failed")
            self._turn_mgr.transition(turn.turn_id, TurnState.ERROR)
            self._pipeline_state = PipelineState.IDLE
            return "[LLM generation failed]"

        self._turn_mgr.record_companion_text(turn.turn_id, response.text)
        self._turn_mgr.transition(turn.turn_id, TurnState.TTS_SYNTHESIZING)

        # Publish LLM response event
        llm_event = LlmResponseGeneratedEvent(
            turn_id=turn.turn_id,
            response_text=response.text,
            model_id=response.model_id,
            model_provider=response.model_provider,
            time_to_first_token_ms=response.time_to_first_token_ms,
            total_latency_ms=response.total_latency_ms,
            token_count=response.token_count,
        )
        await self._bus.publish(llm_event)

        communicated_text = response.text
        if speak:
            try:
                communicated_text = await self._speak_response(turn, response.text)
            except Exception:
                logger.exception("TTS or audio playback failed")
                if turn.is_active:
                    self._turn_mgr.transition(turn.turn_id, TurnState.ERROR)
                self._pipeline_state = PipelineState.IDLE
                return "[Audio playback failed]"
            if not turn.is_active:
                return response.text
            if not communicated_text:
                self._turn_mgr.transition(turn.turn_id, TurnState.ERROR)
                self._pipeline_state = PipelineState.IDLE
                return "[Audio playback failed]"

        self._turn_mgr.transition(turn.turn_id, TurnState.COMPLETED)
        self._pipeline_state = PipelineState.IDLE

        # Publish completed event
        completed_event = ConversationTurnCompletedEvent(
            turn_id=turn.turn_id,
            session_id=self._current_session_id,
            turn_sequence=turn.turn_sequence,
            user_text=text,
            companion_text=communicated_text,
            companion_full_text=response.text,
            total_latency_ms=int((time.time() - t0) * 1000),
            model_id=response.model_id,
        )
        await self._bus.publish(completed_event)
        if self._runtime:
            await self._runtime.commit_response(
                text,
                response,
                completed_event.event_id,
                communicated_text=communicated_text,
            )

        self._metrics.append(
            PipelineMetrics(
                turn_id=turn.turn_id,
                llm_total_ms=int((time.time() - t0) * 1000),
                e2e_latency_ms=turn.total_latency_ms,
            )
        )

        return response.text

    async def _speak_response(self, turn: TurnRecord, text: str) -> str:
        if not self._tts or not self._audio_output:
            logger.error("Spoken turn requires both TTS and audio output")
            return ""

        request = TTSRequest(text=text, turn_id=turn.turn_id)
        provider_name = self._tts.provider_info().name
        first_chunk = True
        try:
            async for chunk in self._tts.synthesize_stream(request):
                if not turn.is_active:
                    break
                if not chunk.audio_bytes:
                    continue
                chunk_text = chunk.text or (text if first_chunk else "")
                duration_ms = chunk.duration_ms or max(
                    1, int(len(chunk.audio_bytes) / (chunk.sample_rate * 2) * 1000)
                )
                record = self._audio_proto.record_synthesis(
                    turn.turn_id,
                    chunk.segment_index,
                    chunk.audio_bytes,
                    duration_ms,
                    chunk_text,
                )
                await self._bus.publish(
                    TtsSynthesizedEvent(
                        turn_id=turn.turn_id,
                        segment_index=chunk.segment_index,
                        text=chunk_text,
                        audio_duration_ms=duration_ms,
                        time_to_first_byte_ms=chunk.time_to_first_byte_ms,
                        tts_provider=provider_name,
                    )
                )
                if first_chunk:
                    self._turn_mgr.transition(turn.turn_id, TurnState.PLAYING)
                    self._pipeline_state = PipelineState.SPEAKING
                    first_chunk = False
                playback = await self._audio_output.play(chunk.audio_bytes, chunk.sample_rate)
                confirmed = self._audio_proto.confirm_played(
                    turn.turn_id,
                    chunk.segment_index,
                    playback.played_duration_ms,
                    playback.was_interrupted,
                )
                await self._bus.publish(
                    AudioPlayedEvent(
                        turn_id=turn.turn_id,
                        segment_index=chunk.segment_index,
                        audio_hash=record.audio_hash,
                        played_duration_ms=playback.played_duration_ms,
                        was_interrupted=playback.was_interrupted,
                        played_fraction=confirmed.played_fraction if confirmed else 0.0,
                    )
                )
                if playback.was_interrupted:
                    break
        finally:
            await self._audio_output.finish()
        return self._audio_proto.get_played_text(turn.turn_id)

    async def interrupt(self) -> bool:
        """Interrupt the current turn (barge-in)."""
        current = self._turn_mgr.get_current_turn()
        if not current or not current.is_active:
            return False

        t0 = time.time()
        self._turn_mgr.interrupt_turn(current.turn_id, "user_speech")

        cancellation_tasks: list[Awaitable[object]] = []
        if self._runtime:
            cancellation_tasks.append(self._runtime.cancel_turn(current.turn_id))
        elif self._llm:
            cancellation_tasks.append(self._llm.cancel(current.turn_id))
        if self._tts:
            cancellation_tasks.append(self._tts.cancel(current.turn_id))
        if self._audio_output:
            cancellation_tasks.append(self._audio_output.stop())
        if cancellation_tasks:
            await asyncio.gather(*cancellation_tasks, return_exceptions=True)

        self._pipeline_state = PipelineState.INTERRUPTED

        interruption_ms = int((time.time() - t0) * 1000)

        # Publish interrupt event
        interrupt_event = ConversationTurnInterruptedEvent(
            turn_id=current.turn_id,
            interrupted_at_audio_ms=0,
            new_turn_id="pending",
            reason="user_speech",
        )
        await self._bus.publish(interrupt_event)

        if self._config.log_latency_breakdown:
            logger.info("Turn %s interrupted in %dms", current.turn_id, interruption_ms)

        return True

    # ── Metrics ───────────────────────────────────────────────────────

    def get_latency_stats(self) -> dict[str, Any]:
        """Get aggregate latency statistics."""
        if not self._metrics:
            return {"p50_ms": 0, "p95_ms": 0, "count": 0}

        latencies = sorted(m.e2e_latency_ms for m in self._metrics)
        n = len(latencies)
        p50 = latencies[n // 2] if n > 0 else 0
        p95 = latencies[int(n * 0.95)] if n > 1 else latencies[-1] if n > 0 else 0

        return {
            "p50_ms": p50,
            "p95_ms": p95,
            "count": n,
            "interrupt_rate": sum(1 for m in self._metrics if m.interrupted) / max(1, n),
        }

    def get_current_state(self) -> PipelineState:
        return self._pipeline_state

    # ── Cleanup ───────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Stop the pipeline and release resources."""
        self._is_running = False
        self._pipeline_state = PipelineState.IDLE
        logger.info("Voice pipeline shut down")
