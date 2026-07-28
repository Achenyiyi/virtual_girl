"""Contract tests for network-streamed Azure PCM synthesis."""

from __future__ import annotations

import httpx
import pytest

from companion.providers.implementations.cloud_tts import CloudTTSConfig, CloudTTSProvider
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
