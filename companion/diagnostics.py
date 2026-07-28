"""Safe, structured preflight diagnostics for target-machine acceptance."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from companion.config_loader import DEFAULT_CONFIG_PATH, RuntimeConfig
from companion.providers.base import ProviderHealth
from companion.providers.implementations.cloud_llm import CloudLLMProvider
from companion.providers.implementations.cloud_tts import CloudTTSProvider
from companion.providers.implementations.websocket_avatar import WebSocketAvatarProvider
from companion.providers.implementations.windows_readonly_action import (
    WindowsReadOnlyActionProvider,
)


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
                "exit_code": self.exit_code,
                "summary": self.summary,
                "checks": [asdict(check) for check in self.checks],
            },
            ensure_ascii=False,
            indent=2,
        )


async def run_diagnostics(
    config: RuntimeConfig, *, require_voice: bool = False, online: bool = False
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
        checks.append(
            DiagnosticCheck(
                "llm.credential",
                DiagnosticStatus.FAIL,
                f"Required LLM credential environment variable is unset: "
                f"{config.llm_config.api_key_env}",
                "Inject a rotated credential through the named environment variable.",
            )
        )
    else:
        checks.append(
            DiagnosticCheck(
                "llm.credential",
                DiagnosticStatus.PASS,
                f"LLM credential is available through {config.llm_config.api_key_env}.",
            )
        )

    checks.extend(_voice_checks(config, require_voice=require_voice))
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

    return DiagnosticReport(checks=checks, online=online, voice_required=require_voice)


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
            with sqlite3.connect(uri, uri=True, timeout=2.0) as connection:
                row = connection.execute("PRAGMA quick_check").fetchone()
            if not row or row[0] != "ok":
                raise sqlite3.DatabaseError("SQLite quick_check did not return ok")
            return DiagnosticCheck(
                "memory.sqlite",
                DiagnosticStatus.PASS,
                f"Memory database integrity is valid: {path}",
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
        checks.append(
            DiagnosticCheck(
                "tts.credential",
                DiagnosticStatus.FAIL,
                f"Azure TTS credential environment variable is unset: "
                f"{config.tts_config.api_key_env}",
                "Inject the Azure Speech credential through the named environment variable.",
            )
        )
    else:
        checks.append(
            DiagnosticCheck(
                "tts.credential",
                DiagnosticStatus.PASS,
                f"Azure TTS credential is available through {config.tts_config.api_key_env}.",
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


async def _optional_provider_checks(
    config: RuntimeConfig, *, online: bool
) -> list[DiagnosticCheck]:
    checks: list[DiagnosticCheck] = []
    if config.avatar_config:
        if not config.avatar_config.get_auth_token():
            checks.append(
                DiagnosticCheck(
                    "avatar.credential",
                    DiagnosticStatus.FAIL,
                    f"Enabled avatar token environment variable is unset: "
                    f"{config.avatar_config.auth_token_env}",
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
