"""Concrete provider implementations.

Phase 1 providers:
- CloudLLMProvider: Anthropic Claude / OpenAI via HTTP
- CloudTTSProvider: Azure / Edge TTS
- LocalASRProvider: sherpa-onnx / faster-whisper stubs
- Composite implementations: Fallback chains, model routing
"""

from companion.providers.implementations.cloud_llm import CloudLLMConfig, CloudLLMProvider
from companion.providers.implementations.cloud_tts import CloudTTSConfig, CloudTTSProvider
from companion.providers.implementations.fallback_llm import FallbackLLMProvider
from companion.providers.implementations.faster_whisper_asr import (
    FasterWhisperASRProvider,
    FasterWhisperConfig,
)

__all__ = [
    "CloudLLMProvider",
    "CloudLLMConfig",
    "CloudTTSProvider",
    "CloudTTSConfig",
    "FallbackLLMProvider",
    "FasterWhisperASRProvider",
    "FasterWhisperConfig",
]
