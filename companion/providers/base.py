"""Base provider types shared across all provider interfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ProviderHealth(StrEnum):
    """Health status of a provider instance."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"  # Working but slow or with errors
    UNHEALTHY = "unhealthy"  # Not responding
    UNKNOWN = "unknown"


class ProviderCapability(StrEnum):
    """Standard capabilities a provider may declare."""

    STREAMING = "streaming"
    BATCH = "batch"
    LOW_LATENCY = "low_latency"
    HIGH_QUALITY = "high_quality"
    OFFLINE = "offline"
    CLOUD = "cloud"
    REAL_TIME = "real_time"
    NON_REAL_TIME = "non_real_time"
    EMOTION_AWARE = "emotion_aware"
    MULTI_LANGUAGE = "multi_language"
    CHINESE = "chinese"
    ENGLISH = "english"


@dataclass
class ProviderInfo:
    """Metadata about a provider implementation."""

    name: str
    version: str
    capabilities: list[ProviderCapability] = field(default_factory=list)
    health: ProviderHealth = ProviderHealth.UNKNOWN
    last_health_check: float = 0.0  # Unix timestamp
    config_schema: dict[str, Any] | None = None


class Provider(ABC):
    """Base class for all providers.

    Every provider must:
    - Declare its info (name, version, capabilities)
    - Provide a health check
    - Support clean shutdown
    """

    @abstractmethod
    def provider_info(self) -> ProviderInfo:
        """Return metadata about this provider."""
        ...

    @abstractmethod
    async def health_check(self) -> ProviderHealth:
        """Check if the provider is operational.

        Should be fast (< 500ms) and not trigger side effects.
        Must update last_health_check timestamp.
        """
        ...

    @abstractmethod
    async def shutdown(self) -> None:
        """Clean shutdown: release resources, close connections."""
        ...
