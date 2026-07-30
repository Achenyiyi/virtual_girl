"""Credential-safe deployment doctor tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
from contextlib import closing

import pytest

from companion.config_loader import RuntimeConfig
from companion.diagnostics import DiagnosticStatus, render_diagnostic_report, run_diagnostics
from companion.memory.memory_service import MemoryServiceConfig
from companion.providers.implementations.cloud_llm import CloudLLMConfig
from companion.providers.implementations.cloud_tts import CloudTTSConfig


def _write_test_vrm(path) -> bytes:
    document = json.dumps(
        {"asset": {"version": "2.0"}, "extensions": {"VRM": {}}},
        separators=(",", ":"),
    ).encode("utf-8")
    document += b" " * (-len(document) % 4)
    content = (
        struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(document))
        + struct.pack("<I4s", len(document), b"JSON")
        + document
    )
    path.write_bytes(content)
    return content


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
async def test_doctor_validates_managed_avatar_installation_without_launching(
    tmp_path, monkeypatch
) -> None:
    executable = tmp_path / "airi.exe"
    content = b"MZapproved-airi"
    executable.write_bytes(content)
    app_asar = tmp_path / "resources" / "app.asar"
    app_asar.parent.mkdir()
    app_asar_content = b"approved-airi-application"
    app_asar.write_bytes(app_asar_content)
    godot = tmp_path / "resources" / "godot-stage" / "godot-stage.exe"
    godot.parent.mkdir()
    godot_content = b"MZapproved-godot-sidecar"
    godot.write_bytes(godot_content)
    model = tmp_path / "nemesia.vrm"
    model_content = _write_test_vrm(model)
    config_path = tmp_path / "companion.yaml"
    config_path.write_text(
        f"""identity:
  avatar_model_id: managed-nemesia
providers:
  llm:
    type: cloud
    cloud:
      provider: openai_compatible
      model: test-model
      api_key_env: TEST_DOCTOR_LLM_KEY
      base_url: https://example.invalid/v1/chat/completions
  memory:
    type: sqlite
    db_path: memory.db
  avatar:
    enabled: true
    type: websocket_bridge
    url: ws://127.0.0.1:6122/ws
    auth_token_env: COMPANION_AVATAR_TOKEN
    launch:
      enabled: true
      executable_path: {executable.as_posix()}
      expected_sha256: {hashlib.sha256(content).hexdigest()}
      expected_app_asar_sha256: {hashlib.sha256(app_asar_content).hexdigest()}
      expected_godot_sha256: {hashlib.sha256(godot_content).hexdigest()}
      model_path: {model.as_posix()}
      expected_model_sha256: {hashlib.sha256(model_content).hexdigest()}
      model_id: managed-nemesia
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_DOCTOR_LLM_KEY", "configured-llm-credential")
    monkeypatch.setenv("COMPANION_AVATAR_TOKEN", "configured-avatar-token")
    monkeypatch.setattr(
        "companion.services.avatar_stage_supervisor._is_remote_path",
        lambda _path, *, platform: False,
    )

    def unexpected_start(*_args, **_kwargs):
        raise AssertionError("doctor must not launch AIRI")

    monkeypatch.setattr(
        "companion.services.avatar_stage_supervisor.subprocess.Popen",
        unexpected_start,
    )

    report = await run_diagnostics(RuntimeConfig.from_yaml(config_path), online=False)

    check = next(
        item for item in report.checks if item.code == "avatar.launch_installation"
    )
    assert check.status == DiagnosticStatus.PASS


@pytest.mark.asyncio
async def test_online_doctor_does_not_launch_or_connect_managed_avatar(
    tmp_path, monkeypatch
) -> None:
    executable = tmp_path / "airi.exe"
    content = b"MZapproved-airi"
    executable.write_bytes(content)
    app_asar = tmp_path / "resources" / "app.asar"
    app_asar.parent.mkdir()
    app_asar_content = b"approved-airi-application"
    app_asar.write_bytes(app_asar_content)
    godot = tmp_path / "resources" / "godot-stage" / "godot-stage.exe"
    godot.parent.mkdir()
    godot_content = b"MZapproved-godot-sidecar"
    godot.write_bytes(godot_content)
    model = tmp_path / "nemesia.vrm"
    model_content = _write_test_vrm(model)
    config_path = tmp_path / "companion.yaml"
    config_path.write_text(
        f"""identity:
  avatar_model_id: managed-nemesia
providers:
  llm:
    type: cloud
    cloud:
      provider: openai_compatible
      model: test-model
      api_key_env: TEST_DOCTOR_LLM_KEY
      base_url: https://example.invalid/v1/chat/completions
  memory:
    type: sqlite
    db_path: memory.db
  avatar:
    enabled: true
    type: websocket_bridge
    url: ws://127.0.0.1:6122/ws
    auth_token_env: COMPANION_AVATAR_TOKEN
    launch:
      enabled: true
      executable_path: {executable.as_posix()}
      expected_sha256: {hashlib.sha256(content).hexdigest()}
      expected_app_asar_sha256: {hashlib.sha256(app_asar_content).hexdigest()}
      expected_godot_sha256: {hashlib.sha256(godot_content).hexdigest()}
      model_path: {model.as_posix()}
      expected_model_sha256: {hashlib.sha256(model_content).hexdigest()}
      model_id: managed-nemesia
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_DOCTOR_LLM_KEY", "configured-llm-credential")
    monkeypatch.setenv("COMPANION_AVATAR_TOKEN", "configured-avatar-token")
    monkeypatch.setattr(
        "companion.services.avatar_stage_supervisor._is_remote_path",
        lambda _path, *, platform: False,
    )

    class HealthyLLM:
        def __init__(self, _config) -> None:
            return None

        async def health_check(self):
            from companion.providers.base import ProviderHealth

            return ProviderHealth.HEALTHY

        async def shutdown(self) -> None:
            return None

    def unexpected_avatar(*_args, **_kwargs):
        raise AssertionError("online doctor must not connect managed AIRI")

    monkeypatch.setattr("companion.diagnostics.CloudLLMProvider", HealthyLLM)
    monkeypatch.setattr(
        "companion.diagnostics.WebSocketAvatarProvider", unexpected_avatar
    )

    report = await run_diagnostics(RuntimeConfig.from_yaml(config_path), online=True)

    check = next(item for item in report.checks if item.code == "avatar.online")
    assert check.status == DiagnosticStatus.WARN
    assert report.exit_code == 0


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
