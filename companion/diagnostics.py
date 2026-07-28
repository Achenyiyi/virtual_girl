"""Safe, structured preflight diagnostics for target-machine acceptance."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from companion.audio.microphone import MicrophoneCapture
from companion.audio.player import SoundDeviceAudioOutput
from companion.config_loader import DEFAULT_CONFIG_PATH, RuntimeConfig
from companion.memory.memory_service import MEMORY_SCHEMA_VERSION, MemoryService
from companion.providers.asr import ASRBatchRequest
from companion.providers.base import ProviderHealth
from companion.providers.implementations.cloud_llm import CloudLLMProvider
from companion.providers.implementations.cloud_tts import CloudTTSProvider
from companion.providers.implementations.faster_whisper_asr import FasterWhisperASRProvider
from companion.providers.implementations.websocket_avatar import WebSocketAvatarProvider
from companion.providers.implementations.windows_readonly_action import (
    WindowsReadOnlyActionProvider,
)
from companion.security.windows_credentials import configured_secret_sources


class DiagnosticStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class DiagnosticCheck:
    code: str
    status: DiagnosticStatus
    message: str
    remediation: str = ""


@dataclass(frozen=True)
class DiagnosticReport:
    checks: list[DiagnosticCheck]
    online: bool
    voice_required: bool
    voice_hardware: bool = False

    @property
    def exit_code(self) -> int:
        return 1 if any(check.status == DiagnosticStatus.FAIL for check in self.checks) else 0

    @property
    def summary(self) -> dict[str, int]:
        return {
            status.value: sum(1 for check in self.checks if check.status == status)
            for status in DiagnosticStatus
        }

    def to_json(self) -> str:
        return json.dumps(
            {
                "online": self.online,
                "voice_required": self.voice_required,
                "voice_hardware": self.voice_hardware,
                "exit_code": self.exit_code,
                "summary": self.summary,
                "checks": [asdict(check) for check in self.checks],
            },
            ensure_ascii=False,
            indent=2,
        )


async def run_diagnostics(
    config: RuntimeConfig,
    *,
    require_voice: bool = False,
    online: bool = False,
    voice_hardware: bool = False,
) -> DiagnosticReport:
    """Inspect configuration and local capabilities without exposing secret values."""
    checks = [_runtime_check(), _packaged_config_check(), _memory_check(config)]
    llm_key_available = bool(config.llm_config and config.llm_config.get_api_key())
    if not config.llm_config:
        checks.append(
            DiagnosticCheck(
                "llm.config",
                DiagnosticStatus.FAIL,
                "Required LLM provider is not configured.",
                "Configure providers.llm.cloud in YAML.",
            )
        )
    elif not llm_key_available:
        sources = configured_secret_sources(
            env_name=config.llm_config.api_key_env,
            credential_target=config.llm_config.credential_target,
        )
        checks.append(
            DiagnosticCheck(
                "llm.credential",
                DiagnosticStatus.FAIL,
                f"Required LLM credential is unavailable from {sources}.",
                "Inject a rotated credential through the configured secure source.",
            )
        )
    else:
        source = config.llm_config.credential_source()
        checks.append(
            DiagnosticCheck(
                "llm.credential",
                DiagnosticStatus.PASS,
                f"LLM credential is available through {source}.",
            )
        )

    checks.extend(_voice_checks(config, require_voice=require_voice))
    if voice_hardware:
        checks.extend(await _voice_hardware_checks(config))
    checks.extend(await _optional_provider_checks(config, online=online))

    if online and config.llm_config and llm_key_available:
        provider = CloudLLMProvider(config.llm_config)
        try:
            health = await provider.health_check()
            checks.append(_health_check("llm.online", "LLM endpoint", health))
        finally:
            await provider.shutdown()

    if online and require_voice and config.tts_config and config.tts_config.get_api_key():
        provider_tts = CloudTTSProvider(config.tts_config)
        try:
            health = await provider_tts.health_check()
            checks.append(_health_check("tts.online", "Azure TTS endpoint", health))
        finally:
            await provider_tts.shutdown()

    return DiagnosticReport(
        checks=checks,
        online=online,
        voice_required=require_voice,
        voice_hardware=voice_hardware,
    )


def render_diagnostic_report(report: DiagnosticReport) -> str:
    """Render a credential-safe human report for the CLI."""
    labels = {
        DiagnosticStatus.PASS: "PASS",
        DiagnosticStatus.WARN: "WARN",
        DiagnosticStatus.FAIL: "FAIL",
        DiagnosticStatus.SKIP: "SKIP",
    }
    lines = ["Virtual Companion doctor"]
    for check in report.checks:
        lines.append(f"[{labels[check.status]}] {check.code}: {check.message}")
        if check.remediation:
            lines.append(f"       fix: {check.remediation}")
    summary = report.summary
    lines.append(
        "Summary: "
        + ", ".join(f"{status}={count}" for status, count in summary.items())
        + f", exit_code={report.exit_code}"
    )
    return "\n".join(lines)


def _runtime_check() -> DiagnosticCheck:
    if sys.platform != "win32" or sys.version_info < (3, 12):
        return DiagnosticCheck(
            "runtime.platform",
            DiagnosticStatus.FAIL,
            f"Unsupported runtime: {sys.platform}, Python {sys.version.split()[0]}.",
            "Use 64-bit Windows 11 and Python 3.12 or newer.",
        )
    return DiagnosticCheck(
        "runtime.platform",
        DiagnosticStatus.PASS,
        f"Windows with Python {sys.version.split()[0]} is supported.",
    )


def _packaged_config_check() -> DiagnosticCheck:
    if DEFAULT_CONFIG_PATH.is_file():
        return DiagnosticCheck(
            "config.packaged",
            DiagnosticStatus.PASS,
            "Packaged default configuration is present.",
        )
    return DiagnosticCheck(
        "config.packaged",
        DiagnosticStatus.FAIL,
        "Packaged default configuration is missing.",
        "Reinstall the release wheel and verify companion/resources/default.yaml.",
    )


def _memory_check(config: RuntimeConfig) -> DiagnosticCheck:
    path = Path(config.effective_memory_config().db_path).expanduser().resolve()
    try:
        if path.exists():
            uri = path.as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as connection:
                row = connection.execute("PRAGMA quick_check").fetchone()
                if not row or row[0] != "ok":
                    raise sqlite3.DatabaseError("SQLite quick_check did not return ok")
                schema_version, legacy = MemoryService.validate_connection_schema(
                    connection, allow_legacy=True
                )
            return DiagnosticCheck(
                "memory.sqlite",
                DiagnosticStatus.PASS,
                (
                    f"Memory database integrity is valid; legacy schema will be registered "
                    f"as v{MEMORY_SCHEMA_VERSION} on startup: {path}"
                    if legacy
                    else f"Memory database schema v{schema_version} is valid: {path}"
                ),
            )
        parent = _nearest_existing_parent(path.parent)
        if os.access(parent, os.W_OK):
            return DiagnosticCheck(
                "memory.sqlite",
                DiagnosticStatus.PASS,
                f"Memory database can be created under: {path.parent}",
            )
        raise PermissionError(f"directory is not writable: {parent}")
    except (OSError, sqlite3.DatabaseError) as exc:
        return DiagnosticCheck(
            "memory.sqlite",
            DiagnosticStatus.FAIL,
            f"Memory database check failed: {type(exc).__name__}",
            "Repair the SQLite database or set COMPANION_DB_PATH to a writable location.",
        )


def _voice_checks(config: RuntimeConfig, *, require_voice: bool) -> list[DiagnosticCheck]:
    if not require_voice:
        return [
            DiagnosticCheck(
                "voice.local",
                DiagnosticStatus.SKIP,
                "Voice dependencies and devices were not requested.",
                "Run with --doctor --voice-input to validate the full local voice prerequisites.",
            )
        ]

    checks: list[DiagnosticCheck] = []
    for module_name in ("faster_whisper", "numpy", "sounddevice"):
        available = importlib.util.find_spec(module_name) is not None
        checks.append(
            DiagnosticCheck(
                f"voice.module.{module_name}",
                DiagnosticStatus.PASS if available else DiagnosticStatus.FAIL,
                f"Voice module {module_name} is {'installed' if available else 'missing'}.",
                "Install the hash-locked voice requirements."
                if not available
                else "",
            )
        )
    if importlib.util.find_spec("sounddevice") is not None:
        checks.extend(_audio_device_checks())

    if not config.tts_config:
        checks.append(
            DiagnosticCheck(
                "tts.config",
                DiagnosticStatus.FAIL,
                "Voice mode requires an enabled Azure TTS provider.",
            )
        )
    elif not config.tts_config.get_api_key():
        sources = configured_secret_sources(
            env_name=config.tts_config.api_key_env,
            credential_target=config.tts_config.credential_target,
        )
        checks.append(
            DiagnosticCheck(
                "tts.credential",
                DiagnosticStatus.FAIL,
                f"Azure TTS credential is unavailable from {sources}.",
                "Inject the Azure Speech credential through the configured secure source.",
            )
        )
    else:
        source = _resolved_secret_source(
            env_name=config.tts_config.api_key_env,
            credential_target=config.tts_config.credential_target,
        )
        checks.append(
            DiagnosticCheck(
                "tts.credential",
                DiagnosticStatus.PASS,
                f"Azure TTS credential is available through {source}.",
            )
        )
    return checks


def _audio_device_checks() -> list[DiagnosticCheck]:
    try:
        sounddevice: Any = importlib.import_module("sounddevice")
        input_device = sounddevice.query_devices(kind="input")
        output_device = sounddevice.query_devices(kind="output")
        input_ok = int(input_device.get("max_input_channels", 0)) >= 1
        output_ok = int(output_device.get("max_output_channels", 0)) >= 1
    except Exception as exc:
        return [
            DiagnosticCheck(
                "voice.devices",
                DiagnosticStatus.FAIL,
                f"Audio device enumeration failed: {type(exc).__name__}",
                "Select valid Windows default input/output devices and retry.",
            )
        ]
    return [
        DiagnosticCheck(
            "voice.input_device",
            DiagnosticStatus.PASS if input_ok else DiagnosticStatus.FAIL,
            "Default microphone exposes an input channel."
            if input_ok
            else "Default microphone has no usable input channel.",
        ),
        DiagnosticCheck(
            "voice.output_device",
            DiagnosticStatus.PASS if output_ok else DiagnosticStatus.FAIL,
            "Default playback device exposes an output channel."
            if output_ok
            else "Default playback device has no usable output channel.",
        ),
    ]


async def _voice_hardware_checks(config: RuntimeConfig) -> list[DiagnosticCheck]:
    """Open production model/device paths only after explicit user opt-in."""
    required = ("faster_whisper", "numpy", "sounddevice")
    if any(importlib.util.find_spec(name) is None for name in required):
        return [
            DiagnosticCheck(
                "voice.hardware",
                DiagnosticStatus.FAIL,
                "Deep voice check cannot run because voice modules are missing.",
                "Install the hash-locked voice requirements and retry.",
            )
        ]

    checks: list[DiagnosticCheck] = []
    if not config.asr_config:
        checks.append(
            DiagnosticCheck(
                "voice.model",
                DiagnosticStatus.FAIL,
                "Deep voice check requires faster-whisper configuration.",
            )
        )
    else:
        asr = FasterWhisperASRProvider(config.asr_config)
        started = time.perf_counter()
        try:
            await asyncio.wait_for(asr.preload(), timeout=300.0)
            load_elapsed = time.perf_counter() - started
            checks.append(
                DiagnosticCheck(
                    "voice.model",
                    DiagnosticStatus.PASS,
                    f"faster-whisper model loaded successfully in {load_elapsed:.1f}s.",
                )
            )
            inference_started = time.perf_counter()
            silent_pcm = b"\x00\x00" * (config.voice_pipeline_config.sample_rate // 2)
            result = await asyncio.wait_for(
                asr.transcribe_batch(
                    ASRBatchRequest(
                        audio_bytes=silent_pcm,
                        sample_rate=config.voice_pipeline_config.sample_rate,
                        language=config.voice_pipeline_config.language,
                        turn_id="doctor-silence",
                    )
                ),
                timeout=60.0,
            )
            inference_elapsed = time.perf_counter() - inference_started
            checks.append(
                DiagnosticCheck(
                    "voice.inference",
                    DiagnosticStatus.PASS,
                    "faster-whisper completed an in-memory silence inference "
                    f"in {inference_elapsed:.1f}s ({len(result.text)} text characters).",
                )
            )
        except Exception as exc:
            model_loaded = any(item.code == "voice.model" for item in checks)
            code = "voice.inference" if model_loaded else "voice.model"
            checks.append(
                DiagnosticCheck(
                    code,
                    DiagnosticStatus.FAIL,
                    f"faster-whisper model/inference check failed: {type(exc).__name__}",
                    "Verify model availability, device/compute settings, disk space, and network.",
                )
            )
        finally:
            await asr.shutdown()

    microphone = MicrophoneCapture(config.microphone_config)
    try:
        if not await microphone.start():
            raise RuntimeError("microphone stream did not become ready")
        await asyncio.sleep(0.35)
        frame_count = microphone.state.total_frames_captured
        if frame_count < 1:
            raise RuntimeError("microphone stream produced no frames")
        checks.append(
            DiagnosticCheck(
                "voice.microphone_stream",
                DiagnosticStatus.PASS,
                f"Microphone stream opened and captured {frame_count} in-memory frames.",
            )
        )
    except Exception as exc:
        checks.append(
            DiagnosticCheck(
                "voice.microphone_stream",
                DiagnosticStatus.FAIL,
                f"Microphone stream check failed: {type(exc).__name__}",
                "Close competing audio applications or select a working default microphone.",
            )
        )
    finally:
        await microphone.stop()

    output = SoundDeviceAudioOutput()
    try:
        sample_rate = config.tts_config.sample_rate if config.tts_config else 24_000
        silent_pcm = b"\x00\x00" * max(1, sample_rate // 50)
        playback = await asyncio.wait_for(output.play(silent_pcm, sample_rate), timeout=5.0)
        checks.append(
            DiagnosticCheck(
                "voice.playback_stream",
                DiagnosticStatus.PASS,
                f"Playback stream opened and accepted {playback.played_duration_ms}ms of silence.",
            )
        )
    except Exception as exc:
        checks.append(
            DiagnosticCheck(
                "voice.playback_stream",
                DiagnosticStatus.FAIL,
                f"Playback stream check failed: {type(exc).__name__}",
                "Select a working default playback device and retry.",
            )
        )
    finally:
        await output.stop()
    return checks


async def _optional_provider_checks(
    config: RuntimeConfig, *, online: bool
) -> list[DiagnosticCheck]:
    checks: list[DiagnosticCheck] = []
    if config.avatar_config:
        if not config.avatar_config.get_auth_token():
            sources = configured_secret_sources(
                env_name=config.avatar_config.auth_token_env,
                credential_target=config.avatar_config.credential_target,
            )
            checks.append(
                DiagnosticCheck(
                    "avatar.credential",
                    DiagnosticStatus.FAIL,
                    f"Enabled avatar token is unavailable from {sources}.",
                )
            )
        elif online:
            avatar = WebSocketAvatarProvider(config.avatar_config)
            try:
                checks.append(
                    _health_check("avatar.online", "Avatar bridge", await avatar.health_check())
                )
            finally:
                await avatar.shutdown()
        else:
            checks.append(
                DiagnosticCheck(
                    "avatar.online",
                    DiagnosticStatus.WARN,
                    "Avatar is enabled but connectivity was not tested.",
                    "Run --doctor-online to verify the bridge.",
                )
            )
    else:
        checks.append(
            DiagnosticCheck("avatar.config", DiagnosticStatus.SKIP, "Avatar bridge is disabled.")
        )

    if config.action_provider_config:
        action = WindowsReadOnlyActionProvider(config.action_provider_config)
        try:
            checks.append(
                _health_check(
                    "action.local",
                    "Windows read-only actions",
                    await action.health_check(),
                )
            )
        finally:
            await action.shutdown()
    else:
        checks.append(
            DiagnosticCheck(
                "action.config", DiagnosticStatus.SKIP, "Windows read-only actions are disabled."
            )
        )
    return checks


def _resolved_secret_source(*, env_name: str, credential_target: str) -> str:
    from companion.security.windows_credentials import resolve_secret

    resolved = resolve_secret(
        env_name=env_name, credential_target=credential_target
    )
    return resolved.source or configured_secret_sources(
        env_name=env_name, credential_target=credential_target
    )


def _health_check(code: str, label: str, health: ProviderHealth) -> DiagnosticCheck:
    return DiagnosticCheck(
        code,
        DiagnosticStatus.PASS if health == ProviderHealth.HEALTHY else DiagnosticStatus.FAIL,
        f"{label} health is {health}.",
        f"Repair {label} configuration or connectivity and retry."
        if health != ProviderHealth.HEALTHY
        else "",
    )


def _nearest_existing_parent(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current
