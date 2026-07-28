"""Credential-safe deployment doctor tests."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from companion.config_loader import RuntimeConfig
from companion.diagnostics import DiagnosticStatus, render_diagnostic_report, run_diagnostics
from companion.memory.memory_service import MemoryServiceConfig
from companion.providers.implementations.cloud_llm import CloudLLMConfig
from companion.providers.implementations.cloud_tts import CloudTTSConfig


@pytest.mark.asyncio
async def test_doctor_passes_local_core_without_exposing_credential(tmp_path) -> None:
    secret = "doctor-test-credential-that-must-not-appear"
    config = RuntimeConfig(
        llm_config=CloudLLMConfig(api_key=secret),
        memory_config=MemoryServiceConfig(db_path=str(tmp_path / "memory.db")),
    )

    report = await run_diagnostics(config)
    rendered = render_diagnostic_report(report)
    serialized = report.to_json()

    assert report.exit_code == 0
    assert report.summary["fail"] == 0
    assert secret not in rendered
    assert secret not in serialized
    assert json.loads(serialized)["exit_code"] == 0


@pytest.mark.asyncio
async def test_doctor_fails_for_missing_required_llm_credential(tmp_path) -> None:
    config = RuntimeConfig(
        llm_config=CloudLLMConfig(api_key_env="DOCTOR_MISSING_LLM_KEY"),
        memory_config=MemoryServiceConfig(db_path=str(tmp_path / "memory.db")),
    )

    report = await run_diagnostics(config)

    check = next(item for item in report.checks if item.code == "llm.credential")
    assert report.exit_code == 1
    assert check.status == DiagnosticStatus.FAIL
    assert "DOCTOR_MISSING_LLM_KEY" in check.message


@pytest.mark.asyncio
async def test_doctor_fails_closed_when_runtime_storage_is_not_ready(
    tmp_path, monkeypatch
) -> None:
    config = RuntimeConfig(
        llm_config=CloudLLMConfig(api_key="configured-llm-credential"),
        memory_config=MemoryServiceConfig(db_path=str(tmp_path / "memory.db")),
    )

    def fail_storage(_path):
        raise OSError("insufficient free space")

    monkeypatch.setattr("companion.diagnostics.check_runtime_storage", fail_storage)

    report = await run_diagnostics(config)

    check = next(item for item in report.checks if item.code == "memory.sqlite")
    assert report.exit_code == 1
    assert check.status == DiagnosticStatus.FAIL
    assert check.message == "Memory database check failed: OSError"


@pytest.mark.asyncio
async def test_doctor_rejects_unrelated_or_future_memory_database(tmp_path) -> None:
    for name, initializer in (
        ("unrelated.db", "CREATE TABLE unrelated (value TEXT)"),
        ("future.db", "PRAGMA application_id = 1447251280; PRAGMA user_version = 999"),
    ):
        path = tmp_path / name
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript(initializer)
        config = RuntimeConfig(
            llm_config=CloudLLMConfig(api_key="configured-llm-credential"),
            memory_config=MemoryServiceConfig(db_path=str(path)),
        )

        report = await run_diagnostics(config)
        check = next(item for item in report.checks if item.code == "memory.sqlite")

        assert report.exit_code == 1
        assert check.status == DiagnosticStatus.FAIL


@pytest.mark.asyncio
async def test_voice_doctor_reports_every_missing_prerequisite(tmp_path, monkeypatch) -> None:
    real_find_spec = __import__("importlib.util").util.find_spec

    def fake_find_spec(name):
        if name in {"faster_whisper", "numpy", "sounddevice"}:
            return None
        return real_find_spec(name)

    monkeypatch.setattr("companion.diagnostics.importlib.util.find_spec", fake_find_spec)
    config = RuntimeConfig(
        llm_config=CloudLLMConfig(api_key="configured-llm-credential"),
        tts_config=CloudTTSConfig(api_key_env="DOCTOR_MISSING_TTS_KEY"),
        memory_config=MemoryServiceConfig(db_path=str(tmp_path / "memory.db")),
    )

    report = await run_diagnostics(config, require_voice=True)

    failed_codes = {
        check.code for check in report.checks if check.status == DiagnosticStatus.FAIL
    }
    assert report.exit_code == 1
    assert {
        "voice.module.faster_whisper",
        "voice.module.numpy",
        "voice.module.sounddevice",
        "tts.credential",
    } <= failed_codes


@pytest.mark.asyncio
async def test_online_doctor_uses_provider_health_without_leaking_key(
    tmp_path, monkeypatch
) -> None:
    class HealthyLLM:
        def __init__(self, config) -> None:
            self.config = config

        async def health_check(self):
            from companion.providers.base import ProviderHealth

            return ProviderHealth.HEALTHY

        async def shutdown(self) -> None:
            return None

    monkeypatch.setattr("companion.diagnostics.CloudLLMProvider", HealthyLLM)
    secret = "online-doctor-secret-value"
    config = RuntimeConfig(
        llm_config=CloudLLMConfig(api_key=secret),
        memory_config=MemoryServiceConfig(db_path=str(tmp_path / "memory.db")),
    )

    report = await run_diagnostics(config, online=True)

    online_check = next(check for check in report.checks if check.code == "llm.online")
    assert online_check.status == DiagnosticStatus.PASS
    assert secret not in report.to_json()


@pytest.mark.asyncio
async def test_deep_voice_doctor_runs_model_and_stream_paths(tmp_path, monkeypatch) -> None:
    class FakeASR:
        def __init__(self, config) -> None:
            self.config = config

        async def preload(self) -> None:
            return None

        async def transcribe_batch(self, request):
            from companion.providers.asr import ASRBatchResult

            assert request.audio_bytes
            return ASRBatchResult(text="")

        async def shutdown(self) -> None:
            return None

    class FakeMicrophone:
        class State:
            total_frames_captured = 3

        state = State()

        def __init__(self, config) -> None:
            self.config = config

        async def start(self) -> bool:
            return True

        async def stop(self) -> None:
            return None

    class FakePlayback:
        def __init__(self) -> None:
            return None

        async def play(self, pcm_data, sample_rate):
            from companion.audio.player import PlaybackResult

            assert pcm_data
            assert sample_rate == 24000
            return PlaybackResult(played_duration_ms=20)

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(
        "companion.diagnostics.importlib.util.find_spec", lambda _name: object()
    )
    monkeypatch.setattr("companion.diagnostics.FasterWhisperASRProvider", FakeASR)
    monkeypatch.setattr("companion.diagnostics.MicrophoneCapture", FakeMicrophone)
    monkeypatch.setattr("companion.diagnostics.SoundDeviceAudioOutput", FakePlayback)
    monkeypatch.setattr("companion.diagnostics.asyncio.sleep", _no_sleep)
    config = RuntimeConfig(
        llm_config=CloudLLMConfig(api_key="configured-llm-credential"),
        tts_config=CloudTTSConfig(api_key="configured-tts-credential"),
        memory_config=MemoryServiceConfig(db_path=str(tmp_path / "memory.db")),
    )
    from companion.providers.implementations.faster_whisper_asr import FasterWhisperConfig

    config.asr_config = FasterWhisperConfig()

    report = await run_diagnostics(config, require_voice=True, voice_hardware=True)

    statuses = {check.code: check.status for check in report.checks}
    assert report.voice_hardware
    assert statuses["voice.model"] == DiagnosticStatus.PASS
    assert statuses["voice.inference"] == DiagnosticStatus.PASS
    assert statuses["voice.microphone_stream"] == DiagnosticStatus.PASS
    assert statuses["voice.playback_stream"] == DiagnosticStatus.PASS


async def _no_sleep(_delay) -> None:
    return None
