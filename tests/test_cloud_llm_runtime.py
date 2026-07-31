from __future__ import annotations

import asyncio

import httpx
import pytest

from companion.providers.implementations.cloud_llm import CloudLLMConfig, CloudLLMProvider
from companion.providers.model import LLMRequest


def _request(turn_id: str = "turn") -> LLMRequest:
    return LLMRequest(messages=[{"role": "user", "content": "hello"}], turn_id=turn_id)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@example.com/v1/chat/completions",
        "https://example.com/v1/chat/completions?token=secret",
        "https://example.com/v1/chat/completions#fragment",
    ],
)
def test_cloud_llm_rejects_credential_bearing_endpoints(url: str) -> None:
    with pytest.raises(ValueError, match="https"):
        CloudLLMConfig(provider="openai_compatible", base_url=url)


@pytest.mark.asyncio
async def test_total_retry_budget_is_hard_even_when_request_ignores_cancellation() -> None:
    release = asyncio.Event()
    provider = CloudLLMProvider(
        CloudLLMConfig(
            provider="openai_compatible",
            base_url="https://example.invalid/v1/chat/completions",
            api_key="configured-test-credential",
            max_retries=3,
            retry_delay_seconds=0,
            timeout_seconds=0.01,
        )
    )
    calls = 0

    async def non_cooperative(
        _request: LLMRequest, _api_key: str, stream: bool
    ) -> dict[str, object]:
        del stream
        nonlocal calls
        calls += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
        return {"text": "late"}

    provider._make_api_call = non_cooperative  # type: ignore[method-assign]
    try:
        response = await asyncio.wait_for(provider.generate(_request()), timeout=0.5)
        assert response.finish_reason == "error"
        assert calls == 1
        assert provider.error_rate == 1.0
    finally:
        release.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_read_timeout_is_not_retried_because_completion_is_ambiguous() -> None:
    provider = CloudLLMProvider(
        CloudLLMConfig(
            provider="openai_compatible",
            base_url="https://example.invalid/v1/chat/completions",
            api_key="configured-test-credential",
            max_retries=3,
            retry_delay_seconds=0,
            timeout_seconds=1,
        )
    )
    calls = 0

    async def read_timeout(
        _request: LLMRequest, _api_key: str, stream: bool
    ) -> dict[str, object]:
        del stream
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("response body stalled")

    provider._make_api_call = read_timeout  # type: ignore[method-assign]
    response = await provider.generate(_request())

    assert response.finish_reason == "error"
    assert calls == 1


@pytest.mark.asyncio
async def test_connect_failure_is_retried_within_total_budget() -> None:
    provider = CloudLLMProvider(
        CloudLLMConfig(
            provider="openai_compatible",
            base_url="https://example.invalid/v1/chat/completions",
            api_key="configured-test-credential",
            max_retries=3,
            retry_delay_seconds=0,
            timeout_seconds=1,
        )
    )
    calls = 0

    async def eventually_succeeds(
        _request: LLMRequest, _api_key: str, stream: bool
    ) -> dict[str, object]:
        nonlocal calls
        del stream
        calls += 1
        if calls < 3:
            raise httpx.ConnectError("connection refused")
        return {"text": "ok", "model": "test-model"}

    provider._make_api_call = eventually_succeeds  # type: ignore[method-assign]
    response = await provider.generate(_request())

    assert response.text == "ok"
    assert response.finish_reason == "stop"
    assert calls == 3


@pytest.mark.asyncio
async def test_client_error_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, request=request)

    provider = CloudLLMProvider(
        CloudLLMConfig(
            provider="openai_compatible",
            base_url="https://example.invalid/v1/chat/completions",
            api_key="configured-test-credential",
            max_retries=3,
            retry_delay_seconds=0,
            timeout_seconds=1,
        )
    )
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        response = await provider.generate(_request())
        assert response.finish_reason == "error"
        assert calls == 1
    finally:
        await provider.shutdown()


@pytest.mark.asyncio
async def test_cancel_aborts_stream_before_first_token() -> None:
    entered = asyncio.Event()
    provider = CloudLLMProvider(
        CloudLLMConfig(
            provider="openai_compatible",
            base_url="https://example.invalid/v1/chat/completions",
            api_key="configured-test-credential",
            timeout_seconds=1,
        )
    )

    async def blocked_stream(_request: LLMRequest, _api_key: str):
        entered.set()
        await asyncio.Event().wait()
        yield "unreachable"

    provider._make_streaming_call = blocked_stream  # type: ignore[method-assign]

    async def consume() -> list[str]:
        return [chunk.text async for chunk in provider.generate_stream(_request("stream-turn"))]

    consumer = asyncio.create_task(consume())
    await entered.wait()
    assert await provider.cancel("stream-turn")
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert not provider._active_tasks


@pytest.mark.asyncio
async def test_cancel_unknown_turn_does_not_poison_future_reused_id() -> None:
    provider = CloudLLMProvider(
        CloudLLMConfig(
            provider="openai_compatible",
            base_url="https://example.invalid/v1/chat/completions",
            api_key="configured-test-credential",
            timeout_seconds=1,
        )
    )

    async def one_chunk(_request: LLMRequest, _api_key: str):
        yield "ok"

    provider._make_streaming_call = one_chunk  # type: ignore[method-assign]

    assert not await provider.cancel("reused")
    chunks = [chunk.text async for chunk in provider.generate_stream(_request("reused"))]
    assert chunks == ["ok", ""]


@pytest.mark.asyncio
async def test_stream_yields_chunks_without_buffering_full_response() -> None:
    release_second = asyncio.Event()
    provider = CloudLLMProvider(
        CloudLLMConfig(
            provider="openai_compatible",
            base_url="https://example.invalid/v1/chat/completions",
            api_key="configured-test-credential",
            timeout_seconds=1,
        )
    )

    async def paced_stream(_request: LLMRequest, _api_key: str):
        yield "first"
        await release_second.wait()
        yield "second"

    provider._make_streaming_call = paced_stream  # type: ignore[method-assign]
    iterator = provider.generate_stream(_request("paced")).__aiter__()

    first = await asyncio.wait_for(anext(iterator), timeout=0.5)
    assert first.text == "first"
    release_second.set()
    remaining = [chunk.text async for chunk in iterator]
    assert remaining == ["second", ""]


@pytest.mark.asyncio
async def test_stream_failure_raises_instead_of_yielding_error_as_assistant_text() -> None:
    provider = CloudLLMProvider(
        CloudLLMConfig(
            provider="openai_compatible",
            base_url="https://example.invalid/v1/chat/completions",
            api_key="configured-test-credential",
            timeout_seconds=1,
        )
    )

    async def failed_stream(_request: LLMRequest, _api_key: str):
        raise httpx.ReadError("private response details")
        yield "unreachable"  # pragma: no cover

    provider._make_streaming_call = failed_stream  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Streaming LLM request failed"):
        _ = [chunk async for chunk in provider.generate_stream(_request("failed-stream"))]
