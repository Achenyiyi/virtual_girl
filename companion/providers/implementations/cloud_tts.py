"""Cloud TTS provider backed by Fish Audio.

The runtime keeps ASR local and sends only assistant reply text to Fish Audio TTS.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx

from companion.providers.base import ProviderCapability, ProviderHealth, ProviderInfo
from companion.providers.tts import (
    TTSChunk,
    TTSProvider,
    TTSRequest,
    TTSVoice,
)
from companion.security.windows_credentials import configured_secret_sources, resolve_secret

logger = logging.getLogger(__name__)
T = TypeVar("T")


class CloudTTSError(RuntimeError):
    """Sanitized cloud synthesis failure safe for logs and user-facing errors."""


@dataclass
class CloudTTSConfig:
    """Configuration for Fish Audio cloud TTS."""

    provider: str = "fish_audio"
    model: str = "s2.1-pro-free"
    reference_id: str = ""
    api_key: str = ""
    api_key_env: str = "FISH_API_KEY"
    credential_target: str = ""
    base_url: str = "https://api.fish.audio"
    sample_rate: int = 24000
    timeout_seconds: float = 15.0
    latency: str = "normal"

    def __post_init__(self) -> None:
        configured_secret_sources(
            env_name=self.api_key_env, credential_target=self.credential_target
        )
        if self.provider != "fish_audio":
            raise ValueError("only Fish Audio cloud TTS is currently implemented")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.model):
            raise ValueError("Fish Audio model contains unsupported characters")
        if self.reference_id and not re.fullmatch(r"[A-Za-z0-9_-]+", self.reference_id):
            raise ValueError("Fish Audio reference_id contains unsupported characters")
        if self.sample_rate != 24000:
            raise ValueError("Fish Audio PCM output currently requires 24000 Hz")
        if self.timeout_seconds <= 0:
            raise ValueError("TTS timeout_seconds must be positive")
        if self.latency not in {"low", "normal", "balanced"}:
            raise ValueError("Fish Audio latency must be low, normal, or balanced")
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Fish Audio base_url must use https://")

    def get_api_key(self) -> str:
        if self.api_key:
            return self.api_key
        return resolve_secret(
            env_name=self.api_key_env, credential_target=self.credential_target
        ).value


class CloudTTSProvider(TTSProvider):
    """Cloud-based TTS using Fish Audio."""

    def __init__(self, config: CloudTTSConfig | None = None) -> None:
        self._config = config or CloudTTSConfig()
        self._client: httpx.AsyncClient | None = None
        self._cancelled_syntheses: set[str] = set()
        self._active_streams: dict[str, httpx.Response] = {}
        self._active_tasks: dict[str, asyncio.Task[Any]] = {}

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._config.timeout_seconds)
        return self._client

    async def synthesize(self, request: TTSRequest) -> TTSChunk:
        """Non-streaming synthesis via Fish Audio's REST API."""
        import time

        t0 = time.time()
        owner_task = self._claim_turn(request.turn_id)
        try:
            audio_bytes: bytes = await self._await_bounded(
                self._fish_tts(request), operation_name="synthesis"
            )
            if not audio_bytes:
                raise CloudTTSError("Fish Audio TTS returned empty audio")
        finally:
            self._release_turn(request.turn_id, owner_task)

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
        """Yield PCM bytes as Fish Audio's chunked HTTP response arrives."""
        import time

        t0 = time.time()
        owner_task = self._claim_turn(request.turn_id)
        url, headers, payload = self._fish_request(request)
        chunk_size_bytes = int(self._config.sample_rate * 2 * 0.1)
        segment_idx = 0
        pending: bytes | None = None
        try:
            client = await self._get_client()
            async with client.stream(
                "POST",
                url,
                json=payload,
                headers=headers,
            ) as response:
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
                    yield self._make_stream_chunk(
                        request, pending, segment_idx, t0, is_final=True
                    )
                elif request.turn_id not in self._cancelled_syntheses:
                    raise CloudTTSError("Fish Audio streaming TTS returned empty audio")
        except asyncio.CancelledError:
            raise
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Fish Audio streaming TTS returned HTTP %d", exc.response.status_code
            )
            raise CloudTTSError(
                f"Fish Audio streaming TTS returned HTTP {exc.response.status_code}"
            ) from None
        except httpx.HTTPError:
            logger.error("Fish Audio streaming TTS transport failed")
            raise CloudTTSError("Fish Audio streaming TTS transport failed") from None
        finally:
            self._active_streams.pop(request.turn_id, None)
            self._release_turn(request.turn_id, owner_task)

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
        task = self._active_tasks.get(turn_id)
        active = response is not None or (task is not None and not task.done())
        if not active:
            return False
        self._cancelled_syntheses.add(turn_id)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            task.add_done_callback(self._consume_task_result)
        if response is not None:
            close_task = asyncio.create_task(response.aclose())
            done, _ = await asyncio.wait(
                [close_task], timeout=min(1.0, self._config.timeout_seconds)
            )
            if not done:
                close_task.cancel()
                close_task.add_done_callback(self._consume_task_result)
        return True

    async def list_voices(self) -> list[TTSVoice]:
        """Return the configured Fish Audio voice surface."""
        return [
            TTSVoice(
                voice_id=self._config.reference_id or "fish-audio-default",
                name="Fish Audio S2.1 Pro Free",
                language="zh",
                gender="female",
                style_tags=["natural", "expressive", "free-tier"],
                is_custom=bool(self._config.reference_id),
            )
        ]

    async def _fish_tts(self, request: TTSRequest) -> bytes:
        """Call Fish Audio TTS REST API and return raw PCM bytes."""
        url, headers, payload = self._fish_request(request)
        try:
            client = await self._get_client()
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPStatusError as e:
            logger.error("Fish Audio TTS returned HTTP %d", e.response.status_code)
            raise CloudTTSError(
                f"Fish Audio TTS returned HTTP {e.response.status_code}"
            ) from None
        except httpx.HTTPError:
            logger.error("Fish Audio TTS transport failed")
            raise CloudTTSError("Fish Audio TTS transport failed") from None

    def _fish_request(self, request: TTSRequest) -> tuple[str, dict[str, str], dict[str, Any]]:
        api_key = self._config.get_api_key()
        if not api_key:
            raise CloudTTSError("Fish Audio TTS credential is not configured")
        reference_id = (
            request.voice_id
            if request.voice_id and request.voice_id != "default"
            else self._config.reference_id
        )
        text = self._apply_emotion_tag(request)
        payload: dict[str, Any] = {
            "text": text,
            "format": "pcm",
            "sample_rate": self._config.sample_rate,
            "latency": self._config.latency,
            "normalize": True,
            "prosody": {
                "speed": self._bounded(request.speed, lower=0.5, upper=2.0),
                "volume": self._volume_to_db(request.volume),
                "normalize_loudness": True,
            },
            "chunk_length": 300,
            "min_chunk_length": 50,
            "condition_on_previous_chunks": True,
            "repetition_penalty": 1.2,
        }
        if reference_id:
            payload["reference_id"] = reference_id
        url = f"{self._config.base_url.rstrip('/')}/v1/tts"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "model": self._config.model,
            "User-Agent": "virtual-companion/0.1.0",
        }
        return url, headers, payload

    @staticmethod
    def _bounded(value: float, *, lower: float, upper: float) -> float:
        return max(lower, min(upper, value))

    @staticmethod
    def _volume_to_db(volume: float) -> float:
        return max(-20.0, min(20.0, (volume - 1.0) * 10.0))

    @staticmethod
    def _apply_emotion_tag(request: TTSRequest) -> str:
        if request.text.startswith("["):
            return request.text
        if request.valence > 0.3 and request.arousal > 0.55:
            return f"[excited] {request.text}"
        if request.valence > 0.3:
            return f"[warm, gentle] {request.text}"
        if request.valence < -0.3 and request.arousal > 0.55:
            return f"[worried, tense] {request.text}"
        if request.valence < -0.3:
            return f"[soft, sad] {request.text}"
        if request.arousal > 0.75:
            return f"[energetic] {request.text}"
        return request.text

    def _claim_turn(self, turn_id: str) -> asyncio.Task[Any]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("cloud TTS requires an asyncio task")
        if turn_id:
            existing = self._active_tasks.get(turn_id)
            if existing is not None and not existing.done() and existing is not task:
                raise CloudTTSError("Fish Audio TTS turn is already active")
            self._active_tasks[turn_id] = task
            self._cancelled_syntheses.discard(turn_id)
        return task

    def _release_turn(self, turn_id: str, task: asyncio.Task[Any]) -> None:
        if turn_id and self._active_tasks.get(turn_id) is task:
            self._active_tasks.pop(turn_id, None)
        self._cancelled_syntheses.discard(turn_id)

    async def _await_bounded(self, operation: Any, *, operation_name: str) -> T:
        task: asyncio.Future[T] = asyncio.ensure_future(operation)
        try:
            done, _ = await asyncio.wait([task], timeout=self._config.timeout_seconds)
        except asyncio.CancelledError:
            task.cancel()
            task.add_done_callback(self._consume_task_result)
            raise
        if not done:
            task.cancel()
            task.add_done_callback(self._consume_task_result)
            raise CloudTTSError(f"Fish Audio TTS {operation_name} timed out")
        return await task

    @staticmethod
    def _consume_task_result(task: asyncio.Future[Any]) -> None:
        if task.cancelled():
            return
        with contextlib.suppress(Exception):
            task.exception()

    def provider_info(self) -> ProviderInfo:
        return ProviderInfo(
            name="cloud-tts-fish-audio",
            version="0.1.0",
            capabilities=[
                ProviderCapability.CLOUD,
                ProviderCapability.STREAMING,
                ProviderCapability.EMOTION_AWARE,
                ProviderCapability.CHINESE,
            ],
        )

    async def health_check(self) -> ProviderHealth:
        """Validate the Fish Audio credential without synthesizing speech."""
        api_key = self._config.get_api_key()
        if not api_key:
            return ProviderHealth.UNHEALTHY
        try:
            client = await self._get_client()
            response = await client.get(
                f"{self._config.base_url.rstrip('/')}/wallet/self/api-credit",
                headers={"Authorization": f"Bearer {api_key}"},
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
        active_tasks = tuple(self._active_tasks.values())
        self._active_tasks.clear()
        for task in active_tasks:
            if task is not asyncio.current_task() and not task.done():
                task.cancel()
                task.add_done_callback(self._consume_task_result)
        active_streams = tuple(self._active_streams.values())
        self._active_streams.clear()
        if active_streams:
            close_tasks = [asyncio.create_task(response.aclose()) for response in active_streams]
            done, pending = await asyncio.wait(
                close_tasks,
                timeout=min(1.0, self._config.timeout_seconds),
            )
            for task in done:
                self._consume_task_result(task)
            for task in pending:
                task.cancel()
                task.add_done_callback(self._consume_task_result)
        if self._client:
            client = self._client
            self._client = None
            close_task = asyncio.create_task(client.aclose())
            done, _ = await asyncio.wait(
                [close_task], timeout=min(1.0, self._config.timeout_seconds)
            )
            if not done:
                close_task.cancel()
                close_task.add_done_callback(self._consume_task_result)
        self._cancelled_syntheses.clear()
