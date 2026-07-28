"""Fallback LLM provider — chains multiple providers with failover.

Per the PLAN, the system uses:
1. Cloud frontier models as primary (high quality)
2. Local quantized models as fallback (offline resilience)

The fallback provider automatically switches when:
- Primary provider is unhealthy
- Primary provider returns errors
- Network is unavailable
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

from companion.providers.base import ProviderCapability, ProviderHealth, ProviderInfo
from companion.providers.model import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
)

logger = logging.getLogger(__name__)


class FallbackLLMProvider(LLMProvider):
    """Chains multiple LLM providers in priority order with automatic failover.

    Example:
        primary = CloudLLMProvider(cloud_config)
        fallback = LocalLLMProvider(local_config)
        chain = FallbackLLMProvider([primary, fallback])
    """

    def __init__(self, providers: list[LLMProvider], health_check_interval: float = 30.0) -> None:
        if not providers:
            msg = "At least one provider is required"
            raise ValueError(msg)
        self._providers = providers
        self._health_check_interval = health_check_interval
        self._health_cache: dict[str, tuple[ProviderHealth, float]] = {}
        self._active_priority: int = 0  # Index of currently active provider

    async def _get_healthy_provider(self) -> LLMProvider:
        """Return the first healthy provider, checking health if needed."""
        import time

        now = time.time()

        # Try in priority order
        for i, provider in enumerate(self._providers):
            info = provider.provider_info()
            cached = self._health_cache.get(info.name)

            # Use cached health if recent
            if cached and (now - cached[1]) < self._health_check_interval:
                health = cached[0]
            else:
                try:
                    health = await provider.health_check()
                    self._health_cache[info.name] = (health, now)
                except Exception:
                    health = ProviderHealth.UNHEALTHY
                    self._health_cache[info.name] = (health, now)

            if health == ProviderHealth.HEALTHY:
                self._active_priority = i
                return provider
            elif health == ProviderHealth.DEGRADED and i == 0:
                # Primary degraded, try fallback
                continue

        # All unhealthy — use primary anyway as last resort
        logger.warning("All LLM providers unhealthy, falling back to primary")
        return self._providers[0]

    # ── LLMProvider interface ─────────────────────────────────────────

    async def generate(self, request: LLMRequest) -> LLMResponse:
        provider = await self._get_healthy_provider()
        try:
            return await provider.generate(request)
        except Exception as e:
            logger.exception("Primary LLM provider failed: %s", e)
            # Try next provider
            for p in self._providers:
                if p is not provider:
                    try:
                        health = await p.health_check()
                        if health == ProviderHealth.HEALTHY:
                            response = await p.generate(request)
                            response.model_provider = "fallback"
                            return response
                    except Exception:
                        continue
            # All failed
            return LLMResponse(
                text="抱歉，所有语言模型都暂时不可用。请稍后再试。",
                turn_id=request.turn_id,
                model_id="fallback-failed",
                model_provider="fallback",
                finish_reason="error",
            )

    async def generate_stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        provider = await self._get_healthy_provider()
        try:
            async for chunk in provider.generate_stream(request):
                yield chunk
        except Exception:
            logger.exception("Streaming LLM failed")
            yield LLMStreamChunk(
                text="[生成中断]",
                turn_id=request.turn_id,
                is_first=True,
                is_final=True,
            )

    async def cancel(self, turn_id: str) -> bool:
        cancelled = False
        for provider in self._providers:
            try:
                if await provider.cancel(turn_id):
                    cancelled = True
            except Exception:
                pass
        return cancelled

    # ── Provider lifecycle ────────────────────────────────────────────

    def provider_info(self) -> ProviderInfo:
        names = [p.provider_info().name for p in self._providers]
        return ProviderInfo(
            name=f"fallback-chain:{'→'.join(names)}",
            version="0.1.0",
            capabilities=[
                ProviderCapability.CLOUD,
                ProviderCapability.OFFLINE,
                ProviderCapability.STREAMING,
            ],
        )

    async def health_check(self) -> ProviderHealth:
        """Healthy if at least one provider is healthy."""
        for p in self._providers:
            try:
                health = await p.health_check()
                if health == ProviderHealth.HEALTHY:
                    return ProviderHealth.HEALTHY
            except Exception:
                pass
        return ProviderHealth.UNHEALTHY

    async def shutdown(self) -> None:
        for p in self._providers:
            with contextlib.suppress(Exception):
                await p.shutdown()
