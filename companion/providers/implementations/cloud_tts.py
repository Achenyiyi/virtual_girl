"""Cloud TTS provider backed by Fish Audio.

The runtime keeps ASR local and sends only assistant reply text to Fish Audio TTS.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx

from companion.providers.base import ProviderCapability, ProviderHealth, ProviderInfo
from companion.providers.tts import (
    TTSChunk,
    TTSProvider,
    TTSRequest,
    TTSTimingSegment,
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
    latency: str = "balanced"
    temperature: float = 0.7
    top_p: float = 0.7
    chunk_length: int = 180
    min_chunk_length: int = 30
    max_new_tokens: int = 1024
    repetition_penalty: float = 1.2
    condition_on_previous_chunks: bool = True
    early_stop_threshold: float = 1.0
    max_text_bytes: int = 480

    def __post_init__(self) -> None:
        configured_secret_sources(
            env_name=self.api_key_env, credential_target=self.credential_target
        )
        if self.provider != "fish_audio":
            raise ValueError("only Fish Audio cloud TTS is currently implemented")
        if self.model not in {"s2.1-pro", "s2.1-pro-free"}:
            raise ValueError("Fish Audio streaming TTS requires an S2.1 model")
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
        if not 0 <= self.temperature <= 1 or not 0 <= self.top_p <= 1:
            raise ValueError("Fish Audio temperature and top_p must be between 0 and 1")
        if not 100 <= self.chunk_length <= 300:
            raise ValueError("Fish Audio chunk_length must be between 100 and 300")
        if not 0 <= self.min_chunk_length <= 100:
            raise ValueError("Fish Audio min_chunk_length must be between 0 and 100")
        if self.max_new_tokens < 1:
            raise ValueError("Fish Audio max_new_tokens must be positive")
        if self.repetition_penalty <= 0:
            raise ValueError("Fish Audio repetition_penalty must be positive")
        if not 0 <= self.early_stop_threshold <= 1:
            raise ValueError("Fish Audio early_stop_threshold must be between 0 and 1")
        if self.max_text_bytes < 100:
            raise ValueError("Fish Audio max_text_bytes must be at least 100")
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
            audio_parts: list[bytes] = []
            for segment_request in self._segment_request(request):
                audio_parts.append(
                    await self._await_bounded(
                        self._fish_tts(segment_request), operation_name="synthesis"
                    )
                )
            audio_bytes = b"".join(audio_parts)
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
        """Yield timestamped PCM chunks from Fish Audio's official SSE endpoint."""
        import time

        t0 = time.time()
        owner_task = self._claim_turn(request.turn_id)
        segment_idx = 0
        audio_offset_ms = 0
        alignment_by_chunk: dict[int, tuple[TTSTimingSegment, ...]] = {}
        try:
            segment_requests = self._segment_request(request)
            for segment_number, segment_request in enumerate(segment_requests):
                is_last_segment = segment_number == len(segment_requests) - 1
                saw_audio = False
                pending: tuple[bytes, int] | None = None
                request_audio_base_ms = audio_offset_ms
                chunk_seq_base = segment_number * 1_000_000
                url, headers, payload = self._fish_request(segment_request, timestamped=True)
                client = await self._get_client()
                async with client.stream(
                    "POST",
                    url,
                    json=payload,
                    headers=headers,
                ) as response:
                    response.raise_for_status()
                    self._active_streams[request.turn_id] = response
                    async for event in self._iter_sse_events(response):
                        if request.turn_id in self._cancelled_syntheses:
                            break
                        audio_bytes, snapshot = self._parse_timestamp_event(
                            event,
                            request_audio_base_ms=request_audio_base_ms,
                            chunk_seq_base=chunk_seq_base,
                        )
                        if snapshot is not None:
                            chunk_seq, segments = snapshot
                            alignment_by_chunk[chunk_seq] = segments
                        if not audio_bytes:
                            continue
                        saw_audio = True
                        timeline = tuple(
                            segment
                            for chunk_seq in sorted(alignment_by_chunk)
                            for segment in alignment_by_chunk[chunk_seq]
                        )
                        if pending is not None:
                            pending_audio, pending_start_ms = pending
                            yield self._make_stream_chunk(
                                segment_request,
                                pending_audio,
                                segment_idx,
                                t0,
                                is_final=False,
                                audio_start_ms=pending_start_ms,
                                alignment=timeline,
                            )
                            segment_idx += 1
                        pending = (audio_bytes, audio_offset_ms)
                        audio_offset_ms += self._pcm_duration_ms(audio_bytes)
                    if pending is not None and request.turn_id not in self._cancelled_syntheses:
                        pending_audio, pending_start_ms = pending
                        timeline = tuple(
                            segment
                            for chunk_seq in sorted(alignment_by_chunk)
                            for segment in alignment_by_chunk[chunk_seq]
                        )
                        yield self._make_stream_chunk(
                            segment_request,
                            pending_audio,
                            segment_idx,
                            t0,
                            is_final=is_last_segment,
                            audio_start_ms=pending_start_ms,
                            alignment=timeline,
                            include_text=True,
                        )
                        segment_idx += 1
                    elif not saw_audio and request.turn_id not in self._cancelled_syntheses:
                        raise CloudTTSError("Fish Audio streaming TTS returned empty audio")
                self._active_streams.pop(request.turn_id, None)
                if request.turn_id in self._cancelled_syntheses:
                    break
            if segment_idx and request.turn_id not in self._cancelled_syntheses:
                return
            if request.turn_id not in self._cancelled_syntheses:
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
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            logger.error("Fish Audio timestamp stream returned invalid data")
            raise CloudTTSError("Fish Audio timestamp stream returned invalid data") from None
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
        audio_start_ms: int = 0,
        alignment: tuple[TTSTimingSegment, ...] = (),
        include_text: bool = False,
    ) -> TTSChunk:
        import time

        is_first = segment_index == 0
        return TTSChunk(
            audio_bytes=audio_bytes,
            turn_id=request.turn_id,
            segment_index=segment_index,
            sample_rate=self._config.sample_rate,
            is_first=is_first,
            is_final=is_final,
            text=request.text if include_text else "",
            duration_ms=int(len(audio_bytes) / (self._config.sample_rate * 2 / 1000)),
            time_to_first_byte_ms=(int((time.time() - started_at) * 1000) if is_first else 0),
            audio_start_ms=audio_start_ms,
            alignment=alignment,
        )

    async def cancel(self, turn_id: str) -> bool:
        response = self._active_streams.get(turn_id)
        task = self._active_tasks.get(turn_id)
        active = response is not None or (task is not None and not task.done())
        if not active:
            return False
        self._cancelled_syntheses.add(turn_id)
        if response is not None:
            close_task = asyncio.create_task(response.aclose())
            done, _ = await asyncio.wait(
                [close_task], timeout=min(1.0, self._config.timeout_seconds)
            )
            if not done:
                close_task.cancel()
                close_task.add_done_callback(self._consume_task_result)
        elif task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            task.add_done_callback(self._consume_task_result)
        return True

    async def list_voices(self) -> list[TTSVoice]:
        """Return the configured Fish Audio voice surface."""
        return [
            TTSVoice(
                voice_id=self._config.reference_id or "fish-audio-default",
                name=(
                    "Fish Audio S2.1 Pro Free"
                    if self._config.model == "s2.1-pro-free"
                    else "Fish Audio S2.1 Pro"
                ),
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

    def _fish_request(
        self, request: TTSRequest, *, timestamped: bool = False
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
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
            "temperature": self._config.temperature,
            "top_p": self._config.top_p,
            "prosody": {
                "speed": self._bounded(request.speed, lower=0.5, upper=2.0),
                "volume": self._volume_to_db(request.volume),
                "normalize_loudness": True,
            },
            "chunk_length": self._config.chunk_length,
            "min_chunk_length": self._config.min_chunk_length,
            "max_new_tokens": self._config.max_new_tokens,
            "condition_on_previous_chunks": self._config.condition_on_previous_chunks,
            "repetition_penalty": self._config.repetition_penalty,
            "early_stop_threshold": self._config.early_stop_threshold,
        }
        if reference_id:
            payload["reference_id"] = reference_id
        endpoint = "/v1/tts/stream/with-timestamp" if timestamped else "/v1/tts"
        url = f"{self._config.base_url.rstrip('/')}{endpoint}"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "model": self._config.model,
            "User-Agent": "virtual-companion/0.1.0",
        }
        if timestamped:
            headers["Accept"] = "text/event-stream"
        return url, headers, payload

    async def _iter_sse_events(self, response: httpx.Response) -> AsyncIterator[str]:
        data_lines: list[str] = []
        async for line in response.aiter_lines():
            if line == "":
                if data_lines:
                    yield "\n".join(data_lines)
                    data_lines.clear()
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            yield "\n".join(data_lines)

    def _parse_timestamp_event(
        self,
        payload: str,
        *,
        request_audio_base_ms: int = 0,
        chunk_seq_base: int = 0,
    ) -> tuple[bytes, tuple[int, tuple[TTSTimingSegment, ...]] | None]:
        event = json.loads(payload)
        if not isinstance(event, dict):
            raise ValueError("timestamp event must be an object")
        encoded_audio = event.get("audio_base64", "")
        if not isinstance(encoded_audio, str):
            raise ValueError("audio_base64 must be a string")
        audio_bytes = base64.b64decode(encoded_audio, validate=True) if encoded_audio else b""
        alignment = event.get("alignment")
        if alignment is None:
            return audio_bytes, None
        if not isinstance(alignment, dict):
            raise ValueError("alignment must be an object")
        chunk_seq = chunk_seq_base + int(event["chunk_seq"])
        offset_ms = request_audio_base_ms + self._seconds_to_ms(
            event.get("chunk_audio_offset_sec", 0)
        )
        raw_segments = alignment.get("segments")
        if not isinstance(raw_segments, list):
            raise ValueError("alignment segments must be a list")
        raw_timing: list[tuple[str, int, int]] = []
        for raw_segment in raw_segments:
            if not isinstance(raw_segment, dict):
                raise ValueError("alignment segment must be an object")
            text = raw_segment.get("text")
            if not isinstance(text, str):
                raise ValueError("alignment text must be a string")
            start_ms = offset_ms + self._seconds_to_ms(raw_segment.get("start", 0))
            end_ms = offset_ms + self._seconds_to_ms(raw_segment.get("end", 0))
            if start_ms < 0 or end_ms < start_ms:
                raise ValueError("alignment times are invalid")
            raw_timing.append((text, start_ms, end_ms))
        content = event.get("content", "")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        aligned_text = self._restore_alignment_text(content, [item[0] for item in raw_timing])
        segments = tuple(
            TTSTimingSegment(
                text=segment_text,
                start_ms=start_ms,
                end_ms=end_ms,
                chunk_seq=chunk_seq,
            )
            for segment_text, (_, start_ms, end_ms) in zip(
                aligned_text, raw_timing, strict=True
            )
        )
        return audio_bytes, (chunk_seq, segments)

    @staticmethod
    def _restore_alignment_text(content: str, segment_texts: list[str]) -> list[str]:
        if not segment_texts or not content:
            return segment_texts
        restored: list[str] = []
        cursor = 0
        for segment_text in segment_texts:
            index = content.find(segment_text, cursor)
            if index < 0:
                return segment_texts
            end = index + len(segment_text)
            restored.append(content[cursor:end])
            cursor = end
        if cursor < len(content):
            restored[-1] += content[cursor:]
        return restored

    def _pcm_duration_ms(self, audio_bytes: bytes) -> int:
        return int(len(audio_bytes) / (self._config.sample_rate * 2) * 1000)

    @staticmethod
    def _seconds_to_ms(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("timestamp must be numeric")
        return round(float(value) * 1000)

    def _segment_request(self, request: TTSRequest) -> list[TTSRequest]:
        return [
            replace(request, text=text, segment_index=request.segment_index + index)
            for index, text in enumerate(self._split_text_for_fish(request.text))
        ]

    def _split_text_for_fish(self, text: str) -> list[str]:
        limit = max(1, self._config.max_text_bytes)
        if len(text.encode("utf-8")) <= limit:
            return [text]
        pieces: list[str] = []
        cursor = 0
        for boundary in re.finditer(r"[。！？!?；;]+[^\S\n]*|\n+", text):
            pieces.append(text[cursor : boundary.end()])
            cursor = boundary.end()
        if cursor < len(text):
            pieces.append(text[cursor:])
        pieces = [piece for piece in pieces if piece] or [text]
        segments: list[str] = []
        current = ""
        for piece in pieces:
            candidate = f"{current}{piece}" if current else piece
            if len(candidate.encode("utf-8")) <= limit:
                current = candidate
                continue
            if current:
                segments.append(current)
            current = ""
            segments.extend(self._split_oversized_piece(piece, limit))
        if current:
            segments.append(current)
        return segments or [text]

    @staticmethod
    def _split_oversized_piece(piece: str, limit: int) -> list[str]:
        segments: list[str] = []
        current = ""
        for char in piece:
            candidate = f"{current}{char}"
            if len(candidate.encode("utf-8")) <= limit:
                current = candidate
                continue
            if current:
                segments.append(current)
            current = char
        if current:
            segments.append(current)
        return segments

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
        """Validate the credential and configured voice without synthesizing speech."""
        api_key = self._config.get_api_key()
        if not api_key:
            return ProviderHealth.UNHEALTHY
        try:
            client = await self._get_client()
            headers = {"Authorization": f"Bearer {api_key}"}
            response = await client.get(
                f"{self._config.base_url.rstrip('/')}/wallet/self/api-credit",
                headers=headers,
                timeout=5.0,
            )
            wallet_health = self._health_from_status(response.status_code)
            if wallet_health != ProviderHealth.HEALTHY:
                return wallet_health
            if not self._config.reference_id:
                return ProviderHealth.HEALTHY
            voice_response = await client.get(
                f"{self._config.base_url.rstrip('/')}/model/{self._config.reference_id}",
                headers=headers,
                timeout=5.0,
            )
            voice_health = self._health_from_status(voice_response.status_code)
            if voice_health != ProviderHealth.HEALTHY:
                return voice_health
            voice = voice_response.json()
            if not isinstance(voice, dict) or voice.get("state") != "trained":
                return ProviderHealth.UNHEALTHY
            return ProviderHealth.HEALTHY
        except httpx.ConnectError:
            return ProviderHealth.UNHEALTHY
        except httpx.TimeoutException:
            return ProviderHealth.DEGRADED
        except Exception:
            return ProviderHealth.UNHEALTHY

    @staticmethod
    def _health_from_status(status_code: int) -> ProviderHealth:
        if 200 <= status_code < 300:
            return ProviderHealth.HEALTHY
        if status_code == 429 or 500 <= status_code < 600:
            return ProviderHealth.DEGRADED
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
