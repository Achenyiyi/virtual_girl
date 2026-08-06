"""ASR provider interface — speech recognition for user input.

Supports:
- Real-time streaming ASR (low latency for conversation)
- Offline batch ASR (higher accuracy for memory archiving)
- Pre-roll buffer (300-500ms before VAD trigger, per AIRI #2092)
- Language detection
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from companion.providers.base import Provider, ProviderHealth, ProviderInfo


@dataclass
class ASRStreamRequest:
    """Start a streaming ASR session."""

    turn_id: str
    sample_rate: int = 16000
    language: str = "zh"  # 'zh', 'en', 'auto'
    pre_roll_bytes: bytes = field(default_factory=bytes)  # Pre-roll buffer
    interim_results: bool = True  # Whether to emit partial transcripts


@dataclass
class ASRResult:
    """A speech recognition result (interim or final)."""

    text: str
    turn_id: str
    is_final: bool = False
    confidence: float = 1.0
    language: str = "zh"
    start_offset_ms: int = 0  # Offset into audio stream
    end_offset_ms: int = 0


@dataclass
class ASRBatchRequest:
    """A batch (offline) ASR request for higher accuracy."""

    audio_bytes: bytes
    sample_rate: int = 16000
    language: str = "zh"
    turn_id: str = ""


@dataclass
class ASRBatchResult:
    """Batch ASR result with timing information."""

    text: str
    language: str = "zh"
    confidence: float = 1.0
    segments: list[dict[str, Any]] = field(default_factory=list)  # Timestamped segments
    duration_ms: int = 0
    processing_time_ms: int = 0


class ASRProvider(Provider):
    """Abstract interface for automatic speech recognition providers.

    Implementations:
    - StreamingASRProvider: sherpa-onnx, cloud streaming ASR
    - BatchASRProvider: faster-whisper, cloud batch ASR
    - FallbackASRProvider: chains real-time → offline for accuracy
    """

    @abstractmethod
    def stream_recognize(self, request: ASRStreamRequest) -> AsyncIterator[ASRResult]:
        """Stream audio bytes and receive results as they're recognized.

        The caller pushes audio chunks via a queue and reads results.
        The iterator ends when the caller signals end-of-speech.
        """
        ...

    @abstractmethod
    async def push_audio(self, turn_id: str, audio_bytes: bytes) -> None:
        """Push audio data into an active streaming session."""
        ...

    @abstractmethod
    async def end_audio(self, turn_id: str) -> None:
        """Signal end of audio for a streaming session."""
        ...

    @abstractmethod
    async def cancel(self, turn_id: str) -> bool:
        """Cancel an active streaming session."""
        ...

    @abstractmethod
    async def transcribe_batch(self, request: ASRBatchRequest) -> ASRBatchResult:
        """Offline transcription for higher accuracy (memory archiving, corrections)."""
        ...

    @abstractmethod
    def provider_info(self) -> ProviderInfo: ...

    async def preload(self) -> None:
        """Load heavyweight resources before readiness checks.

        Local model providers override this so `health_check` can report
        HEALTHY once the model is resident in memory. The default no-op keeps
        cloud-backed implementations cheap.
        """

    @abstractmethod
    async def health_check(self) -> ProviderHealth: ...

    @abstractmethod
    async def shutdown(self) -> None: ...
