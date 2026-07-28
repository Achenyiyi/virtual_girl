"""Conversation events — the turn-by-turn dialogue ledger.

Tracks every conversation turn lifecycle:
1. turn.started    — user starts speaking (VAD trigger or text input)
2. turn.interrupted — user interrupts while companion is speaking
3. turn.completed  — turn fully resolved, audio played and confirmed

Key invariant: only audio that was actually played to the user
enters the shared conversation history (via turn.completed).
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from companion.events.base import BaseEvent


class ConversationTurnStartedEvent(BaseEvent):
    """A new conversation turn has begun (user started speaking or typed)."""

    __event_type__: ClassVar[str] = "conversation.turn.started"

    session_id: str = Field(..., description="Session identifier")
    turn_id: str = Field(..., description="Unique turn identifier (ULID)")
    turn_sequence: int = Field(..., ge=0, description="Monotonic turn number within session")
    input_modality: str = Field(
        default="voice",
        description="How the user gave input: 'voice', 'text', 'gesture'",
    )
    audio_start_offset_ms: int | None = Field(
        default=None,
        description="Pre-roll audio offset in ms (captured before VAD trigger)",
    )


class AsrFinalizedEvent(BaseEvent):
    """ASR produced a final transcript for a segment of user speech."""

    __event_type__: ClassVar[str] = "conversation.asr.finalized"

    turn_id: str = Field(..., description="Parent turn identifier")
    segment_index: int = Field(..., ge=0)
    transcript: str = Field(..., min_length=1)
    language: str = Field(default="zh", description="Detected language code")
    confidence: float = Field(..., ge=0, le=1)
    latency_ms: int = Field(..., description="End-of-speech to finalized text latency")


class LlmResponseGeneratedEvent(BaseEvent):
    """The LLM produced a text response for the companion."""

    __event_type__: ClassVar[str] = "conversation.llm.response"

    turn_id: str = Field(..., description="Parent turn identifier")
    response_text: str = Field(..., min_length=1)
    model_id: str = Field(..., description="Model identifier (e.g., 'claude-sonnet-5')")
    model_provider: str = Field(default="cloud", description="'cloud' or 'local'")
    time_to_first_token_ms: int = Field(..., ge=0)
    total_latency_ms: int = Field(..., ge=0)
    token_count: int = Field(..., ge=0)
    interruption_marker: bool = Field(
        default=False,
        description="True if this turn was interrupted before completion",
    )


class TtsSynthesizedEvent(BaseEvent):
    """TTS produced audio for a segment of the companion's response."""

    __event_type__: ClassVar[str] = "conversation.tts.synthesized"

    turn_id: str = Field(..., description="Parent turn identifier")
    segment_index: int = Field(..., ge=0)
    text: str = Field(..., description="Text that was synthesized")
    audio_duration_ms: int = Field(..., ge=0)
    time_to_first_byte_ms: int = Field(..., ge=0)
    tts_provider: str = Field(..., description="TTS engine identifier")
    emotion_params: dict[str, float] = Field(
        default_factory=dict,
        description="Emotion params used for synthesis (valence, arousal, etc.)",
    )


class AudioPlayedEvent(BaseEvent):
    """A segment of TTS audio was actually played through the speakers.

    This is the canonical record of what the user heard. Only text
    whose audio was fully played enters shared conversation history.
    """

    __event_type__: ClassVar[str] = "conversation.audio.played"

    turn_id: str = Field(..., description="Parent turn identifier")
    segment_index: int = Field(..., ge=0)
    audio_hash: str = Field(..., description="SHA-256 of the audio bytes played")
    played_duration_ms: int = Field(..., ge=0)
    was_interrupted: bool = Field(
        default=False,
        description="True if playback was interrupted mid-segment",
    )
    played_fraction: float = Field(
        default=1.0,
        ge=0,
        le=1,
        description="Fraction of the segment that was actually played",
    )


class ConversationTurnInterruptedEvent(BaseEvent):
    """The user interrupted the companion mid-speech (barge-in)."""

    __event_type__: ClassVar[str] = "conversation.turn.interrupted"

    turn_id: str = Field(..., description="Turn that was interrupted")
    interrupted_at_audio_ms: int = Field(..., description="Offset into audio when interrupted")
    new_turn_id: str = Field(..., description="New turn that caused the interruption")
    reason: str = Field(
        default="user_speech",
        description="Why the interruption happened: 'user_speech', 'system_event', 'timeout'",
    )


class ConversationTurnCompletedEvent(BaseEvent):
    """A turn is fully resolved — audio played, no more interruptions.

    This is the event that commits the turn to shared conversation history.
    Only the text whose audio was actually played is included.
    """

    __event_type__: ClassVar[str] = "conversation.turn.completed"

    turn_id: str = Field(..., description="Turn identifier")
    session_id: str = Field(..., description="Session identifier")
    turn_sequence: int = Field(..., ge=0)
    user_text: str = Field(..., description="Final user transcript for this turn")
    companion_text: str = Field(
        ..., description="What the companion actually said (played audio only)"
    )
    companion_full_text: str = Field(
        default="",
        description="Full generated text (may differ if interrupted)",
    )
    was_interrupted: bool = Field(default=False)
    total_latency_ms: int = Field(..., description="VAD trigger to first audio byte")
    language: str = Field(default="zh")
    model_id: str = Field(default="")

    @property
    def is_complete(self) -> bool:
        """A completed turn is idempotent and safe to add to history."""
        return True
