"""Voice Activity Detection (VAD) — energy-based with pre-roll buffer.

Per the PLAN and AIRI Issue #2092:
- Must maintain a 300-500ms pre-roll buffer to avoid cutting off first words
- Energy-based VAD as baseline; can be replaced with ML-based VAD later
- Configurable thresholds for speech start/end detection
"""

from __future__ import annotations

import collections
import logging
import time
from dataclasses import dataclass
from enum import StrEnum

from companion.audio.dsp import pcm_int16_rms

logger = logging.getLogger(__name__)


class VADState(StrEnum):
    SILENCE = "silence"
    SPEECH = "speech"
    COOLDOWN = "cooldown"  # Brief period after speech ends before triggering end-of-speech


@dataclass
class VADConfig:
    """Configuration for voice activity detection."""

    sample_rate: int = 16000
    frame_duration_ms: int = 20  # Frame size for energy calculation
    speech_start_threshold: float = 0.02  # RMS energy threshold for speech start
    speech_end_threshold: float = 0.01  # RMS energy threshold for speech end
    speech_start_frames: int = 5  # Consecutive frames above threshold to start speech
    speech_end_frames: int = 15  # Consecutive frames below threshold to end speech (~300ms)
    pre_roll_buffer_ms: int = 400  # Buffer before VAD trigger
    cooldown_ms: int = 500  # Minimum silence between utterances
    max_speech_duration_ms: int = 30_000  # Max speech before auto-end


@dataclass
class VADResult:
    """Result of VAD processing a frame."""

    state: VADState = VADState.SILENCE
    speech_detected: bool = False
    speech_started: bool = False  # Rising edge
    speech_ended: bool = False  # Falling edge
    rms_energy: float = 0.0
    speech_duration_ms: int = 0
    frame_index: int = 0


class VoiceActivityDetector:
    """Energy-based voice activity detector with pre-roll buffer.

    The pre-roll buffer stores audio BEFORE the VAD triggers, so when
    speech is detected, the ASR receives audio from ~400ms before the
    trigger point, preventing first-word truncation.
    """

    def __init__(self, config: VADConfig | None = None) -> None:
        self._config = config or VADConfig()
        self._state = VADState.SILENCE
        self._speech_frame_count = 0
        self._silence_frame_count = 0
        self._total_frames = 0
        self._speech_start_time: float | None = None

        # Pre-roll buffer: stores audio frames before VAD trigger
        pre_roll_frames = int(self._config.pre_roll_buffer_ms / self._config.frame_duration_ms)
        self._pre_roll: collections.deque[bytes] = collections.deque(maxlen=pre_roll_frames)

        # Cooldown tracking
        self._last_speech_end_time: float = 0.0

    def process_frame(self, audio_frame: bytes) -> VADResult:
        """Process a single audio frame and return VAD state.

        Args:
            audio_frame: Raw PCM audio bytes for one frame
                         (16-bit, mono, sample_rate from config)

        Returns:
            VADResult with current state and edge detection flags
        """
        now = time.time()

        # Store in pre-roll buffer
        self._pre_roll.append(audio_frame)

        # Calculate RMS energy
        rms = pcm_int16_rms(audio_frame)
        self._total_frames += 1
        speech_started = False
        speech_ended = False

        if self._state == VADState.SILENCE:
            if rms > self._config.speech_start_threshold:
                self._speech_frame_count += 1
                if self._speech_frame_count >= self._config.speech_start_frames:
                    self._state = VADState.SPEECH
                    self._speech_start_time = now
                    speech_started = True
                    self._silence_frame_count = 0
                    logger.debug("VAD: speech started (rms=%.4f)", rms)
            else:
                self._speech_frame_count = 0

        elif self._state == VADState.SPEECH:
            speech_duration = (
                (now - self._speech_start_time) * 1000 if self._speech_start_time else 0
            )

            # Check for max duration
            if speech_duration > self._config.max_speech_duration_ms:
                self._state = VADState.COOLDOWN
                speech_ended = True
                self._last_speech_end_time = now
                logger.debug("VAD: speech ended (max duration)")
            elif rms < self._config.speech_end_threshold:
                self._silence_frame_count += 1
                if self._silence_frame_count >= self._config.speech_end_frames:
                    self._state = VADState.COOLDOWN
                    speech_ended = True
                    self._last_speech_end_time = now
                    logger.debug("VAD: speech ended (silence, rms=%.4f)", rms)
            else:
                self._silence_frame_count = 0

        elif self._state == VADState.COOLDOWN:
            cooldown_elapsed = (now - self._last_speech_end_time) * 1000
            if cooldown_elapsed > self._config.cooldown_ms:
                self._state = VADState.SILENCE
                self._speech_frame_count = 0
                self._silence_frame_count = 0
                logger.debug("VAD: cooldown ended, returning to silence")

        speech_duration = (
            int((now - self._speech_start_time) * 1000) if self._speech_start_time else 0
        )

        return VADResult(
            state=self._state,
            speech_detected=self._state == VADState.SPEECH,
            speech_started=speech_started,
            speech_ended=speech_ended,
            rms_energy=rms,
            speech_duration_ms=speech_duration,
            frame_index=self._total_frames,
        )

    def get_pre_roll_audio(self) -> bytes:
        """Get the pre-roll audio buffer (audio before VAD trigger)."""
        return b"".join(self._pre_roll)

    def reset(self) -> None:
        """Reset VAD state for a new utterance."""
        self._state = VADState.SILENCE
        self._speech_frame_count = 0
        self._silence_frame_count = 0
        self._speech_start_time = None
        self._pre_roll.clear()
