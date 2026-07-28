"""Turn management protocol.

Every conversation interaction is a "turn" with a full lifecycle:
1. STARTED — user starts speaking
2. ASR_PROCESSING — speech is being transcribed
3. LLM_THINKING — companion is generating a response
4. TTS_SYNTHESIZING — response is being voiced
5. PLAYING — audio is being played to user
6. COMPLETED — audio played fully, turn committed to history
7. INTERRUPTED — user barged in, turn partially played

Cancellation semantics:
- When a turn is interrupted, in-progress LLM and TTS are cancelled
- Only audio bytes that were actually played are committed
- The new turn takes over immediately
- Audio cleanup (stopping playback) must complete within 300ms (p95)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum

from companion.events.base import generate_ulid


class TurnState(StrEnum):
    """States a conversation turn can be in."""

    STARTED = "started"
    ASR_PROCESSING = "asr_processing"
    LLM_THINKING = "llm_thinking"
    TTS_SYNTHESIZING = "tts_synthesizing"
    PLAYING = "playing"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class TurnRecord:
    """Internal tracking record for a single conversation turn."""

    turn_id: str
    session_id: str
    turn_sequence: int
    state: TurnState = TurnState.STARTED

    # Timestamps (monotonic, milliseconds)
    created_at_ms: int = 0
    first_asr_token_at_ms: int = 0
    asr_finalized_at_ms: int = 0
    first_llm_token_at_ms: int = 0
    llm_completed_at_ms: int = 0
    first_tts_byte_at_ms: int = 0
    first_audio_played_at_ms: int = 0
    audio_completed_at_ms: int = 0
    interrupted_at_ms: int = 0

    # Content
    user_text: str = ""
    companion_text: str = ""
    companion_full_text: str = ""
    audio_segments_played: int = 0

    # Cancellation
    cancelled: bool = False
    cancelled_at_ms: int = 0
    interrupted_by_turn_id: str = ""

    @property
    def total_latency_ms(self) -> int:
        """VAD trigger to first audio byte."""
        if self.created_at_ms and self.first_audio_played_at_ms:
            return self.first_audio_played_at_ms - self.created_at_ms
        return 0

    @property
    def is_active(self) -> bool:
        """Is this turn still in progress?"""
        return self.state not in (
            TurnState.COMPLETED,
            TurnState.INTERRUPTED,
            TurnState.ERROR,
            TurnState.CANCELLED,
        )


class TurnProtocol:
    """Protocol (behavioral contract) for turn lifecycle management.

    This is NOT the implementation — it defines the interface contract
    that the TurnManager must fulfill. It exists to enable testing
    and to document the expected behavior.
    """

    # ── Lifecycle ─────────────────────────────────────────────────────

    @staticmethod
    def validate_turn_id(turn_id: str) -> bool:
        """Turn IDs must be non-empty ULID-like strings."""
        return bool(turn_id) and len(turn_id) >= 26

    @staticmethod
    def validate_session_id(session_id: str) -> bool:
        """Session IDs must be non-empty ULID-like strings."""
        return bool(session_id) and len(session_id) >= 26

    # ── State transitions (allowed transitions) ───────────────────────

    ALLOWED_TRANSITIONS: dict[TurnState, set[TurnState]] = {
        TurnState.STARTED: {
            TurnState.ASR_PROCESSING,
            TurnState.LLM_THINKING,
            TurnState.CANCELLED,
            TurnState.ERROR,
        },
        TurnState.ASR_PROCESSING: {TurnState.LLM_THINKING, TurnState.CANCELLED, TurnState.ERROR},
        TurnState.LLM_THINKING: {
            TurnState.TTS_SYNTHESIZING,
            TurnState.INTERRUPTED,
            TurnState.CANCELLED,
            TurnState.ERROR,
        },
        TurnState.TTS_SYNTHESIZING: {
            TurnState.PLAYING,
            TurnState.INTERRUPTED,
            TurnState.CANCELLED,
            TurnState.ERROR,
        },
        TurnState.PLAYING: {TurnState.COMPLETED, TurnState.INTERRUPTED, TurnState.ERROR},
        TurnState.COMPLETED: set(),  # Terminal
        TurnState.INTERRUPTED: set(),  # Terminal
        TurnState.ERROR: set(),  # Terminal
        TurnState.CANCELLED: set(),  # Terminal
    }

    @staticmethod
    def can_transition(from_state: TurnState, to_state: TurnState) -> bool:
        """Check if a state transition is valid."""
        return to_state in TurnProtocol.ALLOWED_TRANSITIONS.get(from_state, set())


class TurnManager:
    """Manages the lifecycle of conversation turns.

    Handles:
    - Turn ID generation
    - State transitions with validity checking
    - Interruption (barge-in) handling
    - Latency tracking
    """

    def __init__(self) -> None:
        self._turns: dict[str, TurnRecord] = {}
        self._current_turn_id: str | None = None

    def create_turn(self, session_id: str, turn_sequence: int) -> TurnRecord:
        """Create a new turn and set it as current.

        If a previous turn is still active, it gets interrupted.
        """
        # Interrupt current active turn if any
        if self._current_turn_id:
            current = self._turns.get(self._current_turn_id)
            if current and current.is_active:
                self.interrupt_turn(self._current_turn_id, "new_turn_started")

        turn = TurnRecord(
            turn_id=f"turn_{generate_ulid()}",
            session_id=session_id,
            turn_sequence=turn_sequence,
            state=TurnState.STARTED,
            created_at_ms=int(time.time() * 1000),
        )
        self._turns[turn.turn_id] = turn
        self._current_turn_id = turn.turn_id
        return turn

    def transition(self, turn_id: str, new_state: TurnState) -> bool:
        """Attempt a state transition. Returns False if invalid."""
        turn = self._turns.get(turn_id)
        if turn is None:
            return False
        if not TurnProtocol.can_transition(turn.state, new_state):
            return False
        turn.state = new_state
        now_ms = int(time.time() * 1000)

        # Record timing at each transition
        match new_state:
            case TurnState.ASR_PROCESSING:
                turn.first_asr_token_at_ms = now_ms
            case TurnState.LLM_THINKING:
                turn.asr_finalized_at_ms = now_ms
            case TurnState.TTS_SYNTHESIZING:
                turn.llm_completed_at_ms = now_ms
            case TurnState.PLAYING:
                turn.first_audio_played_at_ms = now_ms
            case TurnState.COMPLETED:
                turn.audio_completed_at_ms = now_ms
            case TurnState.INTERRUPTED:
                turn.interrupted_at_ms = now_ms

        return True

    def interrupt_turn(self, turn_id: str, reason: str = "user_speech") -> TurnRecord | None:
        """Mark a turn as interrupted."""
        turn = self._turns.get(turn_id)
        if turn is None:
            return None
        if not turn.is_active:
            return None
        turn.state = TurnState.INTERRUPTED
        turn.interrupted_at_ms = int(time.time() * 1000)
        turn.cancelled = True
        return turn

    def get_turn(self, turn_id: str) -> TurnRecord | None:
        """Get a turn by ID."""
        return self._turns.get(turn_id)

    def get_current_turn(self) -> TurnRecord | None:
        """Get the active turn."""
        if self._current_turn_id:
            return self._turns.get(self._current_turn_id)
        return None

    def get_active_turns(self) -> list[TurnRecord]:
        """Get all currently active turns."""
        return [t for t in self._turns.values() if t.is_active]

    def record_user_text(self, turn_id: str, text: str) -> None:
        """Record the final user text for a turn."""
        turn = self._turns.get(turn_id)
        if turn:
            turn.user_text = text

    def record_companion_text(self, turn_id: str, text: str, is_full: bool = False) -> None:
        """Record companion response text."""
        turn = self._turns.get(turn_id)
        if turn:
            if is_full:
                turn.companion_full_text = text
            else:
                turn.companion_text = text
