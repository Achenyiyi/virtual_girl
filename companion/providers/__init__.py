"""Provider interfaces — the plugin boundary for swappable components.

All external services (ASR, LLM, TTS, memory, avatar, action, perception)
are accessed through abstract provider interfaces. This enables:
- A/B testing of different implementations
- Local/cloud fallback chains
- Vendor independence
- Mock implementations for testing
"""

from companion.providers.action import ActionProvider
from companion.providers.asr import ASRProvider
from companion.providers.avatar import AvatarProvider
from companion.providers.base import ProviderCapability, ProviderHealth, ProviderInfo
from companion.providers.memory import MemoryProvider
from companion.providers.model import LLMProvider
from companion.providers.perception import PerceptionProvider
from companion.providers.tts import TTSProvider

__all__ = [
    "ProviderCapability",
    "ProviderHealth",
    "ProviderInfo",
    "LLMProvider",
    "TTSProvider",
    "ASRProvider",
    "MemoryProvider",
    "AvatarProvider",
    "ActionProvider",
    "PerceptionProvider",
]
