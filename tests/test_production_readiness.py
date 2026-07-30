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
from companion.providers.implementations.cloud_tts import CloudTTSConfig, CloudTTSProvider
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
@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (200, ProviderHealth.HEALTHY),
        (401, ProviderHealth.UNHEALTHY),
        (403, ProviderHealth.UNHEALTHY),
        (429, ProviderHealth.DEGRADED),
        (503, ProviderHealth.DEGRADED),
    ],
)
async def test_tts_health_is_readonly_and_requires_usable_credentials(
    status_code, expected
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code)

    provider = CloudTTSProvider(
        CloudTTSConfig(api_key="test-secret-value-long-enough")
    )
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await provider.health_check() == expected
        assert len(requests) == 1
        assert requests[0].method == "GET"
        assert requests[0].url.path.endswith("/wallet/self/api-credit")
        auth_scheme, auth_value = requests[0].headers["authorization"].split(" ", 1)
        assert auth_scheme == "Bearer"
        assert auth_value == "test-secret-value-long-enough"
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


@pytest.mark.asyncio
async def test_orchestrator_shutdown_records_success() -> None:
    orchestrator = CompanionOrchestrator(
        StateManager(),
        EventBus("test"),
        PolicyGate(),
        llm_provider=MockLLMProvider(),
        memory_provider=MockMemoryProvider(),
    )

    await orchestrator.shutdown()

    assert orchestrator.shutdown_clean


@pytest.mark.asyncio
async def test_orchestrator_shutdown_failure_still_attempts_later_providers() -> None:
    calls: list[str] = []

    class FailingLLM(MockLLMProvider):
        async def shutdown(self) -> None:
            calls.append("llm")
            raise RuntimeError("close failed")

    class TrackingMemory(MockMemoryProvider):
        async def shutdown(self) -> None:
            calls.append("memory")

    orchestrator = CompanionOrchestrator(
        StateManager(),
        EventBus("test"),
        PolicyGate(),
        llm_provider=FailingLLM(),
        memory_provider=TrackingMemory(),
    )

    await orchestrator.shutdown()

    assert calls == ["llm", "memory"]
    assert not orchestrator.shutdown_clean


def test_no_latency_data_is_unknown() -> None:
    health = TelemetryService().get_health_report()
    assert health["latency_ok"]
    assert all(result is None for result in health["latency_ok"].values())
