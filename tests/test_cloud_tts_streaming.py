"""Contract tests for network-streamed Azure PCM synthesis."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from companion.providers.implementations.cloud_tts import (
    CloudTTSConfig,
    CloudTTSError,
    CloudTTSProvider,
)
from companion.providers.tts import TTSRequest


class ChunkedAudioStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"a" * 4800
        yield b"b" * 2400


@pytest.mark.asyncio
async def test_streaming_tts_yields_before_final_chunk() -> None:
    provider = CloudTTSProvider(
        CloudTTSConfig(
            api_key="test-key-long-enough",
            sample_rate=24000,
        )
    )
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, stream=ChunkedAudioStream())
        )
    )
    try:
        chunks = [
            chunk
            async for chunk in provider.synthesize_stream(
                TTSRequest(text="你好", turn_id="turn_stream", sample_rate=24000)
            )
        ]
    finally:
        await provider.shutdown()

    assert len(chunks) == 2
    assert chunks[0].is_first
    assert not chunks[0].is_final
    assert chunks[-1].is_final
    assert b"".join(chunk.audio_bytes for chunk in chunks) == b"a" * 4800 + b"b" * 2400
    assert chunks[0].text == "你好"
    assert chunks[1].text == ""


@pytest.mark.asyncio
async def test_cancel_closes_active_http_stream() -> None:
    class FakeResponse:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    provider = CloudTTSProvider(CloudTTSConfig())
    response = FakeResponse()
    provider._active_streams["turn_cancel"] = response

    assert await provider.cancel("turn_cancel")
    assert response.closed


@pytest.mark.asyncio
async def test_missing_credential_fails_instead_of_yielding_silent_success() -> None:
    provider = CloudTTSProvider(CloudTTSConfig(api_key_env="TEST_MISSING_AZURE_KEY"))

    with pytest.raises(CloudTTSError, match="credential"):
        await provider.synthesize(TTSRequest(text="hello", turn_id="missing"))
    with pytest.raises(CloudTTSError, match="credential"):
        _ = [
            chunk
            async for chunk in provider.synthesize_stream(
                TTSRequest(text="hello", turn_id="missing-stream")
            )
        ]


@pytest.mark.asyncio
async def test_http_error_is_sanitized_and_propagated() -> None:
    provider = CloudTTSProvider(CloudTTSConfig(api_key="configured-test-credential"))
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(401, request=request))
    )
    try:
        with pytest.raises(CloudTTSError, match="HTTP 401"):
            await provider.synthesize(TTSRequest(text="hello", turn_id="http-error"))
        with pytest.raises(CloudTTSError, match="HTTP 401"):
            _ = [
                chunk
                async for chunk in provider.synthesize_stream(
                    TTSRequest(text="hello", turn_id="stream-http-error")
                )
            ]
    finally:
        await provider.shutdown()


@pytest.mark.asyncio
async def test_success_status_with_empty_audio_fails_closed() -> None:
    provider = CloudTTSProvider(CloudTTSConfig(api_key="configured-test-credential"))
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, request=request, content=b"")
        )
    )
    try:
        with pytest.raises(CloudTTSError, match="empty audio"):
            await provider.synthesize(TTSRequest(text="hello", turn_id="empty"))
        with pytest.raises(CloudTTSError, match="empty audio"):
            _ = [
                chunk
                async for chunk in provider.synthesize_stream(
                    TTSRequest(text="hello", turn_id="empty-stream")
                )
            ]
    finally:
        await provider.shutdown()


@pytest.mark.asyncio
async def test_cancel_aborts_stream_before_response_headers() -> None:
    entered = asyncio.Event()
    class BlockingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    provider = CloudTTSProvider(CloudTTSConfig(api_key="configured-test-credential"))
    provider._client = httpx.AsyncClient(transport=BlockingTransport())

    async def consume() -> list[bytes]:
        return [
            chunk.audio_bytes
            async for chunk in provider.synthesize_stream(
                TTSRequest(text="hello", turn_id="pre-header")
            )
        ]

    task = asyncio.create_task(consume())
    await entered.wait()
    try:
        assert await asyncio.wait_for(provider.cancel("pre-header"), timeout=0.5)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.5)
        assert not provider._active_tasks
    finally:
        await provider.shutdown()


@pytest.mark.asyncio
async def test_non_streaming_hard_timeout_bounds_non_cooperative_transport() -> None:
    release = asyncio.Event()

    class CancellationIgnoringTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            return httpx.Response(200, request=request, content=b"late")

    provider = CloudTTSProvider(
        CloudTTSConfig(api_key="configured-test-credential", timeout_seconds=0.01)
    )
    provider._client = httpx.AsyncClient(transport=CancellationIgnoringTransport())
    try:
        with pytest.raises(CloudTTSError, match="timed out"):
            await asyncio.wait_for(
                provider.synthesize(TTSRequest(text="hello", turn_id="hard-timeout")),
                timeout=0.5,
            )
    finally:
        release.set()
        await asyncio.sleep(0)
        await provider.shutdown()


@pytest.mark.asyncio
async def test_duplicate_active_turn_is_rejected() -> None:
    release = asyncio.Event()

    class BlockingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            await release.wait()
            yield b"audio"

    provider = CloudTTSProvider(CloudTTSConfig(api_key="configured-test-credential"))
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, request=request, stream=BlockingStream())
        )
    )

    first = provider.synthesize_stream(TTSRequest(text="one", turn_id="duplicate")).__aiter__()
    first_task = asyncio.create_task(anext(first))
    await asyncio.sleep(0)
    try:
        with pytest.raises(CloudTTSError, match="already active"):
            _ = [
                chunk
                async for chunk in provider.synthesize_stream(
                    TTSRequest(text="two", turn_id="duplicate")
                )
            ]
    finally:
        release.set()
        await first_task
        await first.aclose()
        await provider.shutdown()


@pytest.mark.asyncio
async def test_shutdown_is_bounded_when_stream_close_ignores_cancellation() -> None:
    release = asyncio.Event()

    class CancellationIgnoringResponse:
        async def aclose(self) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()

    provider = CloudTTSProvider(
        CloudTTSConfig(api_key="configured-test-credential", timeout_seconds=0.01)
    )
    provider._active_streams["stuck"] = CancellationIgnoringResponse()  # type: ignore[assignment]
    try:
        await asyncio.wait_for(provider.shutdown(), timeout=0.5)
    finally:
        release.set()
        await asyncio.sleep(0)
