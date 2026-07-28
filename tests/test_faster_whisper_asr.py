"""Tests for the optional faster-whisper adapter without loading a real model."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from companion.providers.asr import ASRBatchRequest
from companion.providers.base import ProviderHealth
from companion.providers.implementations.faster_whisper_asr import (
    FasterWhisperASRProvider,
)


class FakeArray:
    def astype(self, _dtype):
        return self

    def __truediv__(self, _value):
        return self


class FakeNumpy:
    int16 = object()
    float32 = object()

    @staticmethod
    def frombuffer(_audio, dtype):
        return FakeArray()


class FakeModel:
    def transcribe(self, _audio, **_kwargs):
        segment = SimpleNamespace(
            start=0.0,
            end=1.0,
            text=" 你好世界 ",
            avg_logprob=-0.1,
        )
        return [segment], SimpleNamespace(language="zh")


@pytest.mark.asyncio
async def test_batch_transcription_maps_model_result(monkeypatch) -> None:
    provider = FasterWhisperASRProvider()
    provider._model = FakeModel()
    real_import = __import__("importlib").import_module

    def fake_import(name: str):
        return FakeNumpy if name == "numpy" else real_import(name)

    monkeypatch.setattr(
        "companion.providers.implementations.faster_whisper_asr.importlib.import_module",
        fake_import,
    )
    result = await provider.transcribe_batch(
        ASRBatchRequest(
            audio_bytes=b"\x00\x00" * 16_000,
            sample_rate=16_000,
            turn_id="turn_test",
        )
    )
    assert result.text == "你好世界"
    assert result.language == "zh"
    assert result.duration_ms == 1000
    assert 0 < result.confidence <= 1


@pytest.mark.asyncio
async def test_health_is_unhealthy_when_optional_package_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        "companion.providers.implementations.faster_whisper_asr.importlib.util.find_spec",
        lambda _name: None,
    )
    provider = FasterWhisperASRProvider()
    assert await provider.health_check() == ProviderHealth.UNHEALTHY
