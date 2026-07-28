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
import contextlib
import logging
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol, TypeVar

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
    ConversationTurnFailedEvent,
    ConversationTurnInterruptedEvent,
    ConversationTurnStartedEvent,
    LlmResponseGeneratedEvent,
    TtsSynthesizedEvent,
)
from companion.protocols.audio import AudioConfirmationProtocol
from companion.protocols.turn import TurnManager, TurnRecord, TurnState
from companion.providers.asr import ASRBatchRequest, ASRProvider
from companion.providers.model import LLMProvider, LLMRequest
from companion.providers.tts import TTSChunk, TTSProvider, TTSRequest

logger = logging.getLogger(__name__)
T = TypeVar("T")
VoiceFailureStage = Literal[
    "configuration",
    "asr",
    "generation",
    "tts",
    "playback",
    "persistence",
    "cancellation",
]


class VoiceStageError(Exception):
    """Carry a sanitized failure category across the streaming voice stack."""

    def __init__(self, stage: VoiceFailureStage, error_type: str, *, retryable: bool) -> None:
        super().__init__(error_type)
        self.stage = stage
        self.error_type = error_type
        self.retryable = retryable


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
    tts_chunk_timeout_seconds: float = 15.0
    playback_timeout_seconds: float = 30.0
    cleanup_timeout_seconds: float = 2.0
    interrupt_timeout_seconds: float = 0.3
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
        for name, value in (
            ("tts_chunk_timeout_seconds", self.tts_chunk_timeout_seconds),
            ("playback_timeout_seconds", self.playback_timeout_seconds),
            ("cleanup_timeout_seconds", self.cleanup_timeout_seconds),
            ("interrupt_timeout_seconds", self.interrupt_timeout_seconds),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")


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
        self._turn_lock = asyncio.Lock()
        self._active_turn_task: asyncio.Task[Any] | None = None
        self._closed = False

        # Audio input queue (filled by microphone, consumed by pipeline)
        self._audio_input: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)

        # Metrics collection
        self._metrics: list[PipelineMetrics] = []

    # ── Public API ────────────────────────────────────────────────────

    async def start_session(self, session_id: str | None = None) -> str:
        """Start a new conversation session."""
        if self._closed:
            raise RuntimeError("Voice pipeline is shut down")
        self._current_session_id = session_id or f"sess_{generate_ulid()}"
        self._is_running = True
        self._pipeline_state = PipelineState.IDLE
        logger.info("Voice pipeline session started: %s", self._current_session_id)
        return self._current_session_id

    async def stop_session(self) -> None:
        """Stop the current session."""
        self._is_running = False
        await self._stop_active_turn()
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
        async with self._turn_lock:
            self._ensure_accepting_turns()
            self._active_turn_task = asyncio.current_task()
            if not self._is_running:
                await self.start_session()

            turn = await self._begin_turn("text")
            self._turn_mgr.record_user_text(turn.turn_id, text)
            deadline = time.monotonic() + self._config.max_turn_duration_ms / 1000
            try:
                return await self._respond(turn, text, speak=speak, deadline=deadline)
            except asyncio.CancelledError:
                await self._cancel_started_turn(turn)
                raise
            finally:
                if self._active_turn_task is asyncio.current_task():
                    self._active_turn_task = None

    async def process_audio_input(self, audio_bytes: bytes) -> str:
        """Transcribe a completed utterance and run a spoken response turn."""
        async with self._turn_lock:
            self._ensure_accepting_turns()
            self._active_turn_task = asyncio.current_task()
            if not self._is_running:
                await self.start_session()
            turn = await self._begin_turn("voice")
            deadline = time.monotonic() + self._config.max_turn_duration_ms / 1000
            try:
                if not self._asr:
                    await self._fail_turn(
                        turn,
                        stage="configuration",
                        error_type="asr_not_configured",
                        retryable=False,
                    )
                    return "[ASR provider not configured]"

                self._turn_mgr.transition(turn.turn_id, TurnState.ASR_PROCESSING)
                started_at = time.monotonic()
                try:
                    transcript = await self._await_bounded(
                        self._asr.transcribe_batch(
                            ASRBatchRequest(
                                audio_bytes=audio_bytes,
                                sample_rate=self._config.sample_rate,
                                language=self._config.language,
                                turn_id=turn.turn_id,
                            )
                        ),
                        self._remaining_turn_seconds(deadline),
                        stage="asr",
                        timeout_error="turn_timeout",
                    )
                except asyncio.CancelledError:
                    raise
                except VoiceStageError as exc:
                    await self._fail_turn(
                        turn,
                        stage=exc.stage,
                        error_type=exc.error_type,
                        retryable=exc.retryable,
                    )
                    return "[Voice turn timed out]"
                except Exception as exc:
                    logger.exception("ASR transcription failed")
                    await self._fail_turn(
                        turn,
                        stage="asr",
                        error_type=type(exc).__name__,
                        retryable=True,
                    )
                    return "[ASR transcription failed]"

                if not turn.is_active:
                    return ""
                asr_latency_ms = int((time.monotonic() - started_at) * 1000)
                if not transcript.text.strip():
                    await self._fail_turn(
                        turn,
                        stage="asr",
                        error_type="no_speech_recognized",
                        retryable=True,
                    )
                    return "[No speech recognized]"
                self._turn_mgr.record_user_text(turn.turn_id, transcript.text)
                try:
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
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("ASR event persistence failed")
                    await self._fail_turn(
                        turn,
                        stage="persistence",
                        error_type=type(exc).__name__,
                        retryable=True,
                    )
                    return "[Conversation persistence failed]"
                return await self._respond(
                    turn, transcript.text, speak=True, deadline=deadline
                )
            except asyncio.CancelledError:
                await self._cancel_started_turn(turn)
                raise
            finally:
                if self._active_turn_task is asyncio.current_task():
                    self._active_turn_task = None

    async def _begin_turn(self, modality: str) -> TurnRecord:
        self._turn_sequence += 1
        turn = self._turn_mgr.create_turn(self._current_session_id, self._turn_sequence)
        self._pipeline_state = PipelineState.PROCESSING
        try:
            await self._bus.publish(
                ConversationTurnStartedEvent(
                    session_id=self._current_session_id,
                    turn_id=turn.turn_id,
                    turn_sequence=self._turn_sequence,
                    input_modality=modality,
                )
            )
        except asyncio.CancelledError:
            await self._cancel_started_turn(turn)
            raise
        return turn

    async def _respond(
        self,
        turn: TurnRecord,
        text: str,
        *,
        speak: bool,
        deadline: float,
    ) -> str:
        """Generate and optionally speak one already-started turn."""

        if not self._llm and not self._runtime:
            await self._fail_turn(
                turn,
                stage="configuration",
                error_type="llm_not_configured",
                retryable=False,
            )
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
            response = await self._await_bounded(
                response_task,
                self._remaining_turn_seconds(deadline),
                stage="generation",
                timeout_error="turn_timeout",
            )
        except asyncio.CancelledError:
            raise
        except VoiceStageError as exc:
            await self._fail_turn(
                turn,
                stage=exc.stage,
                error_type=exc.error_type,
                retryable=exc.retryable,
            )
            return "[Voice turn timed out]"
        except Exception as exc:
            logger.exception("LLM generation failed")
            await self._fail_turn(
                turn,
                stage="generation",
                error_type=type(exc).__name__,
                retryable=True,
            )
            return "[LLM generation failed]"

        current_turn = self._turn_mgr.get_turn(turn.turn_id)
        if current_turn is None or not current_turn.is_active:
            return response.text

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
        try:
            await self._bus.publish(llm_event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("LLM response event persistence failed")
            await self._fail_turn(
                turn,
                stage="persistence",
                error_type=type(exc).__name__,
                retryable=True,
            )
            return "[Conversation persistence failed]"

        if turn.state == TurnState.INTERRUPTED:
            return response.text

        communicated_text = response.text
        if speak:
            try:
                communicated_text = await self._speak_response(
                    turn, response.text, deadline=deadline
                )
            except VoiceStageError as exc:
                logger.exception("TTS or audio playback failed")
                await self._fail_turn(
                    turn,
                    stage=exc.stage,
                    error_type=exc.error_type,
                    retryable=exc.retryable,
                )
                return "[Audio playback failed]"
            current_turn = self._turn_mgr.get_turn(turn.turn_id)
            if current_turn is None or not current_turn.is_active:
                return response.text
            if not communicated_text:
                await self._fail_turn(
                    turn,
                    stage="configuration",
                    error_type="spoken_output_not_configured",
                    retryable=False,
                )
                return "[Audio playback failed]"

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
        if not self._turn_mgr.transition(turn.turn_id, TurnState.COMPLETED):
            return response.text
        self._pipeline_state = PipelineState.IDLE
        try:
            await self._bus.publish(completed_event)
        except asyncio.CancelledError:
            await self._commit_completed_turn(
                turn,
                text,
                response,
                completed_event,
                communicated_text,
            )
            raise
        except Exception as exc:
            logger.exception("Conversation completion persistence failed")
            await self._fail_turn(
                turn,
                stage="persistence",
                error_type=type(exc).__name__,
                retryable=True,
                allow_completed=True,
            )
            return "[Conversation persistence failed]"
        await self._commit_completed_turn(turn, text, response, completed_event, communicated_text)

        self._metrics.append(
            PipelineMetrics(
                turn_id=turn.turn_id,
                llm_total_ms=int((time.time() - t0) * 1000),
                e2e_latency_ms=turn.total_latency_ms,
            )
        )

        return response.text

    async def _speak_response(self, turn: TurnRecord, text: str, *, deadline: float) -> str:
        if not self._tts or not self._audio_output:
            logger.error("Spoken turn requires both TTS and audio output")
            return ""

        request = TTSRequest(text=text, turn_id=turn.turn_id)
        try:
            provider_name = self._tts.provider_info().name
        except Exception as exc:
            raise VoiceStageError("tts", type(exc).__name__, retryable=True) from exc
        first_chunk = True
        try:
            try:
                stream = self._tts.synthesize_stream(request)
                iterator = stream.__aiter__()
                while True:
                    next_chunk_task: asyncio.Future[TTSChunk] = asyncio.ensure_future(
                        anext(iterator)
                    )
                    try:
                        done_chunk, _ = await asyncio.wait(
                            [next_chunk_task],
                            timeout=min(
                                self._config.tts_chunk_timeout_seconds,
                                self._remaining_turn_seconds(deadline),
                            ),
                        )
                        if not done_chunk:
                            next_chunk_task.cancel()
                            next_chunk_task.add_done_callback(self._consume_future_result)
                            raise VoiceStageError(
                                "tts", "tts_chunk_timeout", retryable=True
                            )
                        chunk = await next_chunk_task
                    except asyncio.CancelledError:
                        next_chunk_task.cancel()
                        next_chunk_task.add_done_callback(self._consume_future_result)
                        raise
                    except StopAsyncIteration:
                        break
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
                    try:
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
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        raise VoiceStageError(
                            "persistence", type(exc).__name__, retryable=True
                        ) from exc
                    current_turn = self._turn_mgr.get_turn(turn.turn_id)
                    if current_turn is None or not current_turn.is_active:
                        break
                    if first_chunk:
                        if not self._turn_mgr.transition(turn.turn_id, TurnState.PLAYING):
                            break
                        self._pipeline_state = PipelineState.SPEAKING
                        first_chunk = False
                    playback_task: asyncio.Future[PlaybackResult] = asyncio.ensure_future(
                        self._audio_output.play(chunk.audio_bytes, chunk.sample_rate)
                    )
                    try:
                        done_playback, _ = await asyncio.wait(
                            [playback_task],
                            timeout=min(
                                self._config.playback_timeout_seconds,
                                self._remaining_turn_seconds(deadline),
                            ),
                        )
                        if not done_playback:
                            playback_task.cancel()
                            playback_task.add_done_callback(self._consume_future_result)
                            raise VoiceStageError(
                                "playback", "playback_timeout", retryable=True
                            )
                        playback = await playback_task
                    except asyncio.CancelledError:
                        playback_task.cancel()
                        playback_task.add_done_callback(self._consume_future_result)
                        raise
                    except VoiceStageError:
                        raise
                    except Exception as exc:
                        raise VoiceStageError(
                            "playback", type(exc).__name__, retryable=True
                        ) from exc
                    confirmed = self._audio_proto.confirm_played(
                        turn.turn_id,
                        chunk.segment_index,
                        playback.played_duration_ms,
                        playback.was_interrupted,
                    )
                    try:
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
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        raise VoiceStageError(
                            "persistence", type(exc).__name__, retryable=True
                        ) from exc
                    if playback.was_interrupted:
                        break
            except (asyncio.CancelledError, VoiceStageError):
                raise
            except Exception as exc:
                raise VoiceStageError("tts", type(exc).__name__, retryable=True) from exc
        finally:
            finish_task: asyncio.Future[None] = asyncio.ensure_future(self._audio_output.finish())
            try:
                done_finish, _ = await asyncio.wait(
                    [finish_task], timeout=self._config.cleanup_timeout_seconds
                )
                if not done_finish:
                    finish_task.cancel()
                    finish_task.add_done_callback(self._consume_future_result)
                    raise VoiceStageError(
                        "playback", "playback_cleanup_timeout", retryable=True
                    )
                await finish_task
            except asyncio.CancelledError:
                finish_task.cancel()
                finish_task.add_done_callback(self._consume_future_result)
                raise
            except Exception as exc:
                if isinstance(exc, VoiceStageError):
                    raise
                raise VoiceStageError("playback", type(exc).__name__, retryable=True) from exc
        return self._audio_proto.get_played_text(turn.turn_id)

    async def _commit_completed_turn(
        self,
        turn: TurnRecord,
        text: str,
        response: Any,
        completed_event: ConversationTurnCompletedEvent,
        communicated_text: str,
    ) -> None:
        async def commit() -> None:
            if self._runtime:
                try:
                    await self._runtime.commit_response(
                        text,
                        response,
                        completed_event.event_id,
                        communicated_text=communicated_text,
                    )
                except Exception:
                    logger.exception(
                        "Post-completion conversation history commit failed for turn %s",
                        turn.turn_id,
                    )

        task = asyncio.create_task(commit())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def _fail_turn(
        self,
        turn: TurnRecord,
        *,
        stage: VoiceFailureStage,
        error_type: str,
        retryable: bool,
        allow_completed: bool = False,
        allow_interrupted: bool = False,
    ) -> None:
        if not self._turn_mgr.fail_turn(
            turn.turn_id,
            allow_completed=allow_completed,
            allow_interrupted=allow_interrupted,
        ):
            return
        self._pipeline_state = PipelineState.IDLE
        await self._bus.publish(
            ConversationTurnFailedEvent(
                turn_id=turn.turn_id,
                session_id=turn.session_id,
                turn_sequence=turn.turn_sequence,
                stage=stage,
                error_type=error_type,
                retryable=retryable,
                elapsed_ms=max(0, int(time.time() * 1000) - turn.created_at_ms),
            )
        )

    async def _cancel_started_turn(self, turn: TurnRecord) -> None:
        if not turn.is_active:
            return
        self._turn_mgr.transition(turn.turn_id, TurnState.CANCELLED)
        self._pipeline_state = PipelineState.IDLE
        await self._bus.publish(
            ConversationTurnFailedEvent(
                turn_id=turn.turn_id,
                session_id=turn.session_id,
                turn_sequence=turn.turn_sequence,
                stage="cancellation",
                error_type="cancelled",
                retryable=True,
                elapsed_ms=max(0, int(time.time() * 1000) - turn.created_at_ms),
            )
        )

    async def interrupt(self) -> bool:
        """Interrupt the current turn (barge-in)."""
        current = self._turn_mgr.get_current_turn()
        if not current or not current.is_active:
            return False

        self._turn_mgr.interrupt_turn(current.turn_id, "user_speech")
        interrupt_task = asyncio.create_task(self._finish_interruption(current))
        try:
            await asyncio.shield(interrupt_task)
        except asyncio.CancelledError:
            await interrupt_task
            raise
        return True

    async def _finish_interruption(self, current: TurnRecord) -> None:
        """Cancel providers and durably finish one already accepted interruption."""
        t0 = time.time()

        cancellation_tasks: list[Awaitable[object]] = []
        if self._runtime:
            cancellation_tasks.append(self._runtime.cancel_turn(current.turn_id))
        elif self._llm:
            cancellation_tasks.append(self._llm.cancel(current.turn_id))
        if self._asr:
            cancellation_tasks.append(self._asr.cancel(current.turn_id))
        if self._tts:
            cancellation_tasks.append(self._tts.cancel(current.turn_id))
        if self._audio_output:
            cancellation_tasks.append(self._audio_output.stop())
        if cancellation_tasks:
            futures = [asyncio.ensure_future(item) for item in cancellation_tasks]
            done, pending = await asyncio.wait(
                futures,
                timeout=self._config.interrupt_timeout_seconds,
            )
            for future in done:
                self._consume_future_result(future)
            if pending:
                for future in pending:
                    future.cancel()
                    future.add_done_callback(self._consume_future_result)
                logger.error(
                    "Turn %s provider cancellation exceeded %.3fs",
                    current.turn_id,
                    self._config.interrupt_timeout_seconds,
                )

        self._pipeline_state = PipelineState.INTERRUPTED

        interruption_ms = int((time.time() - t0) * 1000)

        # Publish interrupt event
        interrupt_event = ConversationTurnInterruptedEvent(
            turn_id=current.turn_id,
            interrupted_at_audio_ms=0,
            new_turn_id="pending",
            reason="user_speech",
        )
        try:
            await self._bus.publish(interrupt_event)
        except Exception as exc:
            logger.exception("Conversation interruption persistence failed")
            await self._fail_turn(
                current,
                stage="persistence",
                error_type=type(exc).__name__,
                retryable=True,
                allow_interrupted=True,
            )

        self._metrics.append(
            PipelineMetrics(
                turn_id=current.turn_id,
                e2e_latency_ms=current.total_latency_ms,
                interrupted=True,
                interruption_response_ms=interruption_ms,
            )
        )

        if self._config.log_latency_breakdown:
            logger.info("Turn %s interrupted in %dms", current.turn_id, interruption_ms)

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

    def _ensure_accepting_turns(self) -> None:
        if self._closed:
            raise RuntimeError("Voice pipeline is shut down")

    @staticmethod
    def _remaining_turn_seconds(deadline: float) -> float:
        return max(0.0, deadline - time.monotonic())

    @staticmethod
    def _consume_future_result(future: asyncio.Future[Any]) -> None:
        if future.cancelled():
            return
        with contextlib.suppress(Exception):
            future.exception()

    async def _await_bounded(
        self,
        operation: Awaitable[T],
        timeout_seconds: float,
        *,
        stage: VoiceFailureStage,
        timeout_error: str,
    ) -> T:
        future: asyncio.Future[T] = asyncio.ensure_future(operation)
        try:
            done, _ = await asyncio.wait([future], timeout=timeout_seconds)
        except asyncio.CancelledError:
            future.cancel()
            future.add_done_callback(self._consume_future_result)
            raise
        if not done:
            future.cancel()
            future.add_done_callback(self._consume_future_result)
            raise VoiceStageError(stage, timeout_error, retryable=True)
        return await future

    async def _stop_active_turn(self) -> None:
        task = self._active_turn_task
        if task is None or task is asyncio.current_task() or task.done():
            return
        current = self._turn_mgr.get_current_turn()
        if current and current.is_active:
            await self.interrupt()
        if not task.done():
            task.cancel()
        done, _ = await asyncio.wait([task], timeout=self._config.cleanup_timeout_seconds)
        if not done:
            task.add_done_callback(self._consume_future_result)
            logger.error(
                "Active voice turn did not stop within %.3fs",
                self._config.cleanup_timeout_seconds,
            )
            return
        self._consume_future_result(task)

    # ── Cleanup ───────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Stop the pipeline and release resources."""
        if self._closed:
            return
        self._closed = True
        self._is_running = False
        await self._stop_active_turn()
        self._pipeline_state = PipelineState.IDLE
        logger.info("Voice pipeline shut down")
