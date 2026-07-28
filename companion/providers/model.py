"""LLM provider interface — the companion's "brain".

Supports:
- Cloud frontier models (high quality, always available)
- Local quantized models (offline fallback, lower quality)
- Streaming token generation with abort/cancel
- Model router that selects provider based on task and availability
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from companion.providers.base import Provider, ProviderHealth, ProviderInfo


@dataclass
class LLMRequest:
    """A request to the LLM for response generation."""

    messages: list[dict[str, str]]  # List of {"role": str, "content": str}
    system_prompt: str = ""
    max_tokens: int = 1024
    temperature: float = 0.7
    stop_sequences: list[str] = field(default_factory=list)
    tools: list[dict[str, Any]] | None = None  # Function definitions for tool calling
    turn_id: str = ""
    model_hint: str | None = None  # Preferred model, if available

    # Context window management
    max_context_tokens: int = 8000
    working_memory_tokens: int = 2000  # Budget for recent conversation
    fact_tokens: int = 1000  # Budget for retrieved facts
    episode_tokens: int = 1500  # Budget for episodic memories


@dataclass
class LLMResponse:
    """A completed LLM response."""

    text: str
    turn_id: str
    model_id: str
    model_provider: str  # 'cloud' or 'local'
    token_count: int = 0
    time_to_first_token_ms: int = 0
    total_latency_ms: int = 0
    finish_reason: str = "stop"  # 'stop', 'length', 'interrupted', 'error'
    tool_calls: list[dict[str, Any]] | None = None


@dataclass
class LLMStreamChunk:
    """A single token or small chunk of a streaming response."""

    text: str
    turn_id: str
    is_first: bool = False
    is_final: bool = False
    token_index: int = 0


class LLMProvider(Provider):
    """Abstract interface for language model providers.

    Implementations:
    - CloudLLMProvider: Anthropic, OpenAI, etc.
    - LocalLLMProvider: llama.cpp, Ollama
    - FallbackLLMProvider: chains multiple providers with failover
    - ModelRouter: selects provider based on task requirements
    """

    @abstractmethod
    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Generate a complete response (non-streaming)."""
        ...

    @abstractmethod
    def generate_stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        """Generate a streaming response.

        Must be cancellable — the caller can break the iterator
        to abort generation (e.g., user interrupted).
        """
        ...

    @abstractmethod
    async def cancel(self, turn_id: str) -> bool:
        """Cancel an in-progress generation for the given turn.

        Returns True if a generation was actually cancelled.
        """
        ...

    @abstractmethod
    def provider_info(self) -> ProviderInfo: ...

    @abstractmethod
    async def health_check(self) -> ProviderHealth: ...

    @abstractmethod
    async def shutdown(self) -> None: ...


class ModelRouter(Protocol):
    """Protocol for selecting which LLM provider to use for a request.

    The router considers:
    - Task priority (can this use local or must use cloud?)
    - Provider health (is cloud reachable?)
    - Latency budget (is this real-time or background?)
    - Quality requirements (is this personality-critical?)
    """

    async def select_provider(self, request: LLMRequest) -> LLMProvider:
        """Choose the best available provider for this request."""
        ...

    async def get_fallback_chain(self, request: LLMRequest) -> list[LLMProvider]:
        """Return ordered list of fallback providers."""
        ...
