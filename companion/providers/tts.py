"""TTS provider interface — voice synthesis for the companion.

Key requirements from the PLAN:
- Streaming: first audio byte within target p50 < 900ms
- Emotion-aware: synthesis parameters driven by affect state
- Cancellable: barge-in must stop playback within p95 < 300ms
- Provider-agnostic: supports CosyVoice (local), GPT-SoVITS (local), cloud TTS
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from companion.providers.base import Provider, ProviderHealth, ProviderInfo


@dataclass
class TTSRequest:
    """A text-to-speech synthesis request."""

    text: str
    turn_id: str
    segment_index: int = 0

    # Emotion parameters (continuous, from affect state)
    valence: float = 0.0  # -1 to 1
    arousal: float = 0.5  # 0 to 1
    energy: float = 0.5  # 0 to 1

    # Voice configuration
    voice_id: str = "default"
    speed: float = 1.0
    volume: float = 1.0
    pitch: float = 1.0

    # Output format
    sample_rate: int = 24000
    audio_format: str = "pcm"  # 'pcm', 'wav', 'mp3', 'opus'


@dataclass
class TTSChunk:
    """A chunk of synthesized audio (streaming)."""

    audio_bytes: bytes
    turn_id: str
    segment_index: int = 0
    sample_rate: int = 24000
    is_first: bool = False
    is_final: bool = False
    text: str = ""  # The text this audio corresponds to
    duration_ms: int = 0
    time_to_first_byte_ms: int = 0


@dataclass
class TTSVoice:
    """A voice available for synthesis."""

    voice_id: str
    name: str
    language: str = "zh"
    gender: str = "female"
    style_tags: list[str] = field(default_factory=list)  # e.g., ['cheerful', 'gentle']
    is_custom: bool = False  # True for GPT-SoVITS custom voices
    preview_audio_path: str | None = None


class TTSProvider(Provider):
    """Abstract interface for text-to-speech providers."""

    @abstractmethod
    async def synthesize(self, request: TTSRequest) -> TTSChunk:
        """Non-streaming synthesis — returns complete audio."""
        ...

    @abstractmethod
    def synthesize_stream(self, request: TTSRequest) -> AsyncIterator[TTSChunk]:
        """Streaming synthesis — yields audio chunks as they're generated.

        Must be cancellable for barge-in support.
        """
        ...

    @abstractmethod
    async def cancel(self, turn_id: str) -> bool:
        """Cancel in-progress synthesis for a turn."""
        ...

    @abstractmethod
    async def list_voices(self) -> list[TTSVoice]:
        """Return available voices."""
        ...

    @abstractmethod
    def provider_info(self) -> ProviderInfo: ...

    @abstractmethod
    async def health_check(self) -> ProviderHealth:
        """Health check MUST include a test synthesis of a short phrase."""
        ...

    @abstractmethod
    async def shutdown(self) -> None: ...
