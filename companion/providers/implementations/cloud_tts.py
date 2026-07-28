"""Cloud TTS provider — Azure Cognitive Services / Edge TTS.

Supports:
- Streaming TTS via Azure Speech SDK / Edge TTS
- Emotion-aware voice modulation
- Cancellable synthesis for barge-in
- Health check with test synthesis
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from companion.providers.base import ProviderCapability, ProviderHealth, ProviderInfo
from companion.providers.tts import (
    TTSChunk,
    TTSProvider,
    TTSRequest,
    TTSVoice,
)

logger = logging.getLogger(__name__)


@dataclass
class CloudTTSConfig:
    """Configuration for cloud TTS."""

    provider: str = "azure"  # 'azure', 'edge', 'openai', 'elevenlabs'
    voice: str = "zh-CN-XiaoxiaoNeural"
    api_key: str = ""
    api_key_env: str = "AZURE_SPEECH_KEY"
    region: str = "eastasia"
    sample_rate: int = 24000
    timeout_seconds: float = 15.0
    default_style: str = "general"  # SSML style

    def __post_init__(self) -> None:
        if self.provider != "azure":
            raise ValueError("only Azure cloud TTS is currently implemented")
        if not re.fullmatch(r"[A-Za-z0-9-]+", self.voice):
            raise ValueError("Azure voice name contains unsupported characters")
        if not re.fullmatch(r"[a-z0-9-]+", self.region):
            raise ValueError("Azure region contains unsupported characters")
        if self.sample_rate != 24000:
            raise ValueError("Azure raw PCM output currently requires a 24000 Hz sample rate")
        if self.timeout_seconds <= 0:
            raise ValueError("TTS timeout_seconds must be positive")

    def get_api_key(self) -> str:
        return self.api_key or os.environ.get(self.api_key_env, "")


class CloudTTSProvider(TTSProvider):
    """Cloud-based TTS using Azure Cognitive Services / Edge TTS."""

    def __init__(self, config: CloudTTSConfig | None = None) -> None:
        self._config = config or CloudTTSConfig()
        self._client: httpx.AsyncClient | None = None
        self._cancelled_syntheses: set[str] = set()
        self._active_streams: dict[str, httpx.Response] = {}

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._config.timeout_seconds)
        return self._client

    # ── Synthesis ─────────────────────────────────────────────────────

    async def synthesize(self, request: TTSRequest) -> TTSChunk:
        """Non-streaming synthesis via Azure TTS REST API."""
        import time

        t0 = time.time()

        ssml = self._build_ssml(request)
        audio_bytes = await self._azure_tts(ssml, request.turn_id)

        duration_ms = 0
        if audio_bytes:
            # Estimate duration from PCM size
            duration_ms = int(len(audio_bytes) / (self._config.sample_rate * 2 / 1000))

        elapsed = int((time.time() - t0) * 1000)
        return TTSChunk(
            audio_bytes=audio_bytes,
            turn_id=request.turn_id,
            segment_index=request.segment_index,
            sample_rate=self._config.sample_rate,
            is_first=True,
            is_final=True,
            text=request.text,
            duration_ms=duration_ms,
            time_to_first_byte_ms=elapsed,
        )

    async def synthesize_stream(self, request: TTSRequest) -> AsyncIterator[TTSChunk]:
        """Yield PCM bytes as Azure's HTTP response body arrives."""
        import time

        t0 = time.time()
        ssml = self._build_ssml(request)
        self._cancelled_syntheses.discard(request.turn_id)
        url, headers = self._azure_request()
        if not headers:
            return
        chunk_size_bytes = int(self._config.sample_rate * 2 * 0.1)
        segment_idx = 0
        pending: bytes | None = None
        try:
            client = await self._get_client()
            async with client.stream("POST", url, content=ssml, headers=headers) as response:
                response.raise_for_status()
                self._active_streams[request.turn_id] = response
                async for chunk_data in response.aiter_bytes(chunk_size_bytes):
                    if request.turn_id in self._cancelled_syntheses:
                        break
                    if pending is not None:
                        yield self._make_stream_chunk(
                            request, pending, segment_idx, t0, is_final=False
                        )
                        segment_idx += 1
                    pending = chunk_data
                if pending is not None and request.turn_id not in self._cancelled_syntheses:
                    yield self._make_stream_chunk(request, pending, segment_idx, t0, is_final=True)
        except httpx.HTTPError:
            logger.exception("Azure streaming TTS request failed")
        finally:
            self._active_streams.pop(request.turn_id, None)
            self._cancelled_syntheses.discard(request.turn_id)

    def _make_stream_chunk(
        self,
        request: TTSRequest,
        audio_bytes: bytes,
        segment_index: int,
        started_at: float,
        *,
        is_final: bool,
    ) -> TTSChunk:
        import time

        is_first = segment_index == 0
        return TTSChunk(
            audio_bytes=audio_bytes,
            turn_id=request.turn_id,
            segment_index=request.segment_index * 1000 + segment_index,
            sample_rate=self._config.sample_rate,
            is_first=is_first,
            is_final=is_final,
            text=request.text if is_first else "",
            duration_ms=int(len(audio_bytes) / (self._config.sample_rate * 2 / 1000)),
            time_to_first_byte_ms=(int((time.time() - started_at) * 1000) if is_first else 0),
        )

    async def cancel(self, turn_id: str) -> bool:
        response = self._active_streams.get(turn_id)
        active = response is not None
        self._cancelled_syntheses.add(turn_id)
        if response is not None:
            await response.aclose()
        return active

    # ── Voice management ──────────────────────────────────────────────

    async def list_voices(self) -> list[TTSVoice]:
        """Return available Azure neural voices."""
        return [
            TTSVoice(
                voice_id="zh-CN-XiaoxiaoNeural",
                name="晓晓",
                language="zh",
                gender="female",
                style_tags=["cheerful", "gentle", "sad", "angry", "fearful"],
            ),
            TTSVoice(
                voice_id="zh-CN-YunxiNeural",
                name="云希",
                language="zh",
                gender="male",
                style_tags=["cheerful", "sad", "angry", "fearful", "disgruntled"],
            ),
            TTSVoice(
                voice_id="zh-CN-XiaoyiNeural",
                name="晓伊",
                language="zh",
                gender="female",
                style_tags=["cheerful", "sad", "angry"],
            ),
        ]

    # ── Internal ──────────────────────────────────────────────────────

    def _build_ssml(self, request: TTSRequest) -> str:
        """Build SSML with emotion and prosody parameters."""
        voice = self._config.voice
        style = self._emotion_to_style(request.valence, request.arousal)

        # Adjust rate and pitch based on emotion.
        # SSML prosody accepts "medium", "slow", "fast", or relative values like "+20%"
        if request.speed == 1.0:
            rate = "medium"
        elif request.speed > 1.0:
            rate = f"+{(request.speed - 1.0) * 100:.0f}%"
        else:
            rate = f"-{(1.0 - request.speed) * 100:.0f}%"

        pitch = "medium" if request.pitch == 1.0 else f"{(request.pitch - 1.0) * 100:+.0f}%"

        # Escape XML special chars in text
        text = (
            request.text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

        ssml = (
            f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            f'xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="zh-CN">'
            f'<voice name="{voice}">'
            f'<mstts:express-as style="{style}" styledegree="0.8">'
            f'<prosody rate="{rate}" pitch="{pitch}" volume="{request.volume * 100:.0f}%">'
            f"{text}"
            f"</prosody>"
            f"</mstts:express-as>"
            f"</voice>"
            f"</speak>"
        )
        return ssml

    @staticmethod
    def _emotion_to_style(valence: float, arousal: float) -> str:
        """Map continuous affect to SSML speaking style."""
        if valence > 0.3:
            return "cheerful" if arousal > 0.5 else "gentle"
        elif valence < -0.3:
            return "sad" if arousal < 0.5 else "fearful"
        elif arousal > 0.7:
            return "excited"
        return "general"

    async def _azure_tts(self, ssml: str, turn_id: str) -> bytes:
        """Call Azure TTS REST API."""
        url, headers = self._azure_request()
        if not headers:
            return b""

        try:
            client = await self._get_client()
            resp = await client.post(url, content=ssml, headers=headers)
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPStatusError as e:
            logger.error("Azure TTS HTTP error: %s", e)
            return b""
        except Exception as e:
            logger.error("Azure TTS error: %s", e)
            return b""

    def _azure_request(self) -> tuple[str, dict[str, str]]:
        api_key = self._config.get_api_key()
        if not api_key:
            logger.warning("Azure TTS: no API key configured")
            return "", {}
        url = f"https://{self._config.region}.tts.speech.microsoft.com/cognitiveservices/v1"
        return url, {
            "Ocp-Apim-Subscription-Key": api_key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "raw-24khz-16bit-mono-pcm",
            "User-Agent": "virtual-companion/0.1.0",
        }

    # ── Provider lifecycle ────────────────────────────────────────────

    def provider_info(self) -> ProviderInfo:
        return ProviderInfo(
            name=f"cloud-tts-{self._config.provider}",
            version="0.1.0",
            capabilities=[
                ProviderCapability.CLOUD,
                ProviderCapability.STREAMING,
                ProviderCapability.EMOTION_AWARE,
                ProviderCapability.CHINESE,
            ],
        )

    async def health_check(self) -> ProviderHealth:
        """Validate the Azure credential and endpoint without synthesizing audio."""
        api_key = self._config.get_api_key()
        if not api_key:
            return ProviderHealth.UNHEALTHY
        try:
            client = await self._get_client()
            url = (
                f"https://{self._config.region}.tts.speech.microsoft.com/"
                "cognitiveservices/voices/list"
            )
            response = await client.get(
                url,
                headers={"Ocp-Apim-Subscription-Key": api_key},
                timeout=5.0,
            )
            if 200 <= response.status_code < 300:
                return ProviderHealth.HEALTHY
            if response.status_code == 429 or 500 <= response.status_code < 600:
                return ProviderHealth.DEGRADED
            return ProviderHealth.UNHEALTHY
        except httpx.ConnectError:
            return ProviderHealth.UNHEALTHY
        except httpx.TimeoutException:
            return ProviderHealth.DEGRADED
        except Exception:
            return ProviderHealth.UNHEALTHY

    async def shutdown(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        self._active_streams.clear()
        self._cancelled_syntheses.clear()
