"""Optional local ASR provider backed by faster-whisper."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from companion.providers.asr import (
    ASRBatchRequest,
    ASRBatchResult,
    ASRProvider,
    ASRResult,
    ASRStreamRequest,
)
from companion.providers.base import ProviderCapability, ProviderHealth, ProviderInfo


@dataclass(frozen=True)
class FasterWhisperConfig:
    model_size: str = "base"
    device: str = "auto"
    compute_type: str = "default"
    cpu_threads: int = 0


class FasterWhisperASRProvider(ASRProvider):
    """Batch ASR plus buffered streaming compatibility for local voice input."""

    def __init__(self, config: FasterWhisperConfig | None = None) -> None:
        self._config = config or FasterWhisperConfig()
        self._model: Any | None = None
        self._model_lock = asyncio.Lock()
        self._buffers: dict[str, bytearray] = {}
        self._end_events: dict[str, asyncio.Event] = {}
        self._cancelled: set[str] = set()

    def provider_info(self) -> ProviderInfo:
        return ProviderInfo(
            name="faster-whisper",
            version="1",
            capabilities=[
                ProviderCapability.OFFLINE,
                ProviderCapability.BATCH,
                ProviderCapability.MULTI_LANGUAGE,
            ],
        )

    async def health_check(self) -> ProviderHealth:
        if importlib.util.find_spec("faster_whisper") is None:
            return ProviderHealth.UNHEALTHY
        return ProviderHealth.HEALTHY if self._model is not None else ProviderHealth.DEGRADED

    async def preload(self) -> None:
        """Load the configured model before entering the interactive voice loop."""
        await self._ensure_model()

    async def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        async with self._model_lock:
            if self._model is None:
                self._model = await asyncio.to_thread(self._load_model)
        return self._model

    def _load_model(self) -> Any:
        try:
            module = importlib.import_module("faster_whisper")
        except ImportError as exc:
            raise RuntimeError(
                "Voice input requires the optional dependency: "
                "pip install 'virtual-companion[voice]'"
            ) from exc
        kwargs: dict[str, Any] = {
            "device": self._config.device,
            "compute_type": self._config.compute_type,
        }
        if self._config.cpu_threads > 0:
            kwargs["cpu_threads"] = self._config.cpu_threads
        return module.WhisperModel(self._config.model_size, **kwargs)

    def stream_recognize(self, request: ASRStreamRequest) -> AsyncIterator[ASRResult]:
        async def generate() -> AsyncIterator[ASRResult]:
            self._buffers[request.turn_id] = bytearray(request.pre_roll_bytes)
            end_event = self._end_events.setdefault(request.turn_id, asyncio.Event())
            try:
                await end_event.wait()
                if request.turn_id in self._cancelled:
                    return
                result = await self.transcribe_batch(
                    ASRBatchRequest(
                        audio_bytes=bytes(self._buffers.get(request.turn_id, b"")),
                        sample_rate=request.sample_rate,
                        language=request.language,
                        turn_id=request.turn_id,
                    )
                )
                if result.text:
                    yield ASRResult(
                        text=result.text,
                        turn_id=request.turn_id,
                        is_final=True,
                        confidence=result.confidence,
                        language=result.language,
                        end_offset_ms=result.duration_ms,
                    )
            finally:
                self._buffers.pop(request.turn_id, None)
                self._end_events.pop(request.turn_id, None)
                self._cancelled.discard(request.turn_id)

        return generate()

    async def push_audio(self, turn_id: str, audio_bytes: bytes) -> None:
        self._buffers.setdefault(turn_id, bytearray()).extend(audio_bytes)

    async def end_audio(self, turn_id: str) -> None:
        self._end_events.setdefault(turn_id, asyncio.Event()).set()

    async def cancel(self, turn_id: str) -> bool:
        active = turn_id in self._buffers or turn_id in self._end_events
        self._cancelled.add(turn_id)
        self._end_events.setdefault(turn_id, asyncio.Event()).set()
        return active

    async def transcribe_batch(self, request: ASRBatchRequest) -> ASRBatchResult:
        if not request.audio_bytes:
            return ASRBatchResult(text="", language=request.language, confidence=0.0)
        model = await self._ensure_model()
        numpy = importlib.import_module("numpy")
        audio = numpy.frombuffer(request.audio_bytes, dtype=numpy.int16)
        audio = audio.astype(numpy.float32) / 32768.0

        def transcribe() -> tuple[list[dict[str, Any]], Any]:
            segments, info = model.transcribe(
                audio,
                language=None if request.language == "auto" else request.language,
                vad_filter=True,
            )
            rows = [
                {
                    "start": segment.start,
                    "end": segment.end,
                    "text": segment.text,
                    "avg_logprob": segment.avg_logprob,
                }
                for segment in segments
            ]
            return rows, info

        started_at = asyncio.get_running_loop().time()
        segments, info = await asyncio.to_thread(transcribe)
        processing_ms = int((asyncio.get_running_loop().time() - started_at) * 1000)
        text = "".join(str(segment["text"]) for segment in segments).strip()
        probabilities = [math.exp(float(segment["avg_logprob"])) for segment in segments]
        confidence = sum(probabilities) / len(probabilities) if probabilities else 0.0
        duration_ms = int(len(request.audio_bytes) / (request.sample_rate * 2) * 1000)
        return ASRBatchResult(
            text=text,
            language=getattr(info, "language", request.language),
            confidence=max(0.0, min(1.0, confidence)),
            segments=segments,
            duration_ms=duration_ms,
            processing_time_ms=processing_ms,
        )

    async def shutdown(self) -> None:
        for event in self._end_events.values():
            event.set()
        self._buffers.clear()
        self._end_events.clear()
        self._cancelled.clear()
        self._model = None
