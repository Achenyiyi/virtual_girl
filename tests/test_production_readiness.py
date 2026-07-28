"""Production readiness gates that must fail closed."""

from __future__ import annotations

import httpx
import pytest

from companion.core.event_bus import EventBus
from companion.core.orchestrator import CompanionOrchestrator
from companion.core.policy_gate import PolicyGate
from companion.core.state_manager import StateManager
from companion.providers.base import ProviderHealth
from companion.providers.implementations.cloud_llm import CloudLLMConfig, CloudLLMProvider
from companion.services.telemetry import TelemetryService
from tests.test_providers import (
    MockActionProvider,
    MockAvatarProvider,
    MockLLMProvider,
    MockMemoryProvider,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (204, ProviderHealth.HEALTHY),
        (401, ProviderHealth.UNHEALTHY),
        (403, ProviderHealth.UNHEALTHY),
        (404, ProviderHealth.UNHEALTHY),
        (429, ProviderHealth.DEGRADED),
        (503, ProviderHealth.DEGRADED),
    ],
)
async def test_cloud_health_requires_a_usable_endpoint(status_code, expected) -> None:
    provider = CloudLLMProvider(
        CloudLLMConfig(
            api_key="test-secret-value-long-enough",
            base_url="https://example.invalid/v1/chat/completions",
        )
    )
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status_code))
    )
    try:
        assert await provider.health_check() == expected
    finally:
        await provider.shutdown()


@pytest.mark.asyncio
async def test_orchestrator_does_not_start_with_unhealthy_required_llm() -> None:
    llm = MockLLMProvider()
    llm._healthy = False
    orchestrator = CompanionOrchestrator(
        StateManager(), EventBus("test"), PolicyGate(), llm_provider=llm
    )

    assert not await orchestrator.startup()
    assert not orchestrator.is_running


@pytest.mark.asyncio
async def test_orchestrator_does_not_start_with_unhealthy_configured_memory() -> None:
    class UnhealthyMemoryProvider(MockMemoryProvider):
        async def health_check(self) -> ProviderHealth:
            return ProviderHealth.UNHEALTHY

    orchestrator = CompanionOrchestrator(
        StateManager(),
        EventBus("test"),
        PolicyGate(),
        llm_provider=MockLLMProvider(),
        memory_provider=UnhealthyMemoryProvider(),
    )

    assert not await orchestrator.startup()
    assert not orchestrator.is_running


@pytest.mark.asyncio
async def test_orchestrator_does_not_start_with_unhealthy_configured_avatar() -> None:
    class UnhealthyAvatarProvider(MockAvatarProvider):
        async def health_check(self) -> ProviderHealth:
            return ProviderHealth.UNHEALTHY

    orchestrator = CompanionOrchestrator(
        StateManager(),
        EventBus("test"),
        PolicyGate(),
        llm_provider=MockLLMProvider(),
        avatar_provider=UnhealthyAvatarProvider(),
    )

    assert not await orchestrator.startup()
    assert not orchestrator.is_running


@pytest.mark.asyncio
async def test_orchestrator_does_not_start_with_unhealthy_configured_action() -> None:
    class UnhealthyActionProvider(MockActionProvider):
        async def health_check(self) -> ProviderHealth:
            return ProviderHealth.UNHEALTHY

    orchestrator = CompanionOrchestrator(
        StateManager(),
        EventBus("test"),
        PolicyGate(),
        llm_provider=MockLLMProvider(),
        action_provider=UnhealthyActionProvider(),
    )

    assert not await orchestrator.startup()
    assert not orchestrator.is_running


def test_no_latency_data_is_unknown() -> None:
    health = TelemetryService().get_health_report()
    assert health["latency_ok"]
    assert all(result is None for result in health["latency_ok"].values())
