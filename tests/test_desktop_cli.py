"""CLI boundary tests for the Python-owned desktop entry point."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

import companion.__main__ as companion_main


def test_desktop_mode_accepts_config_and_log_level_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "desktop.yaml"
    captured: dict[str, Any] = {}

    async def async_main(args: Any) -> int:
        captured["args"] = args
        return 0

    monkeypatch.setattr(companion_main, "_configure_cli_streams", lambda: None)
    monkeypatch.setattr(companion_main, "async_main", async_main)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "companion",
            "--desktop",
            "--config",
            str(config_path),
            "--log-level",
            "DEBUG",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        companion_main.main()

    assert exc_info.value.code == 0
    args = captured["args"]
    assert args.desktop is True
    assert args.config == config_path
    assert args.log_level == "DEBUG"


@pytest.mark.parametrize(
    "incompatible_args",
    (
        ("--once", "hello"),
        ("--voice",),
        ("--voice-input",),
        ("--validate-config",),
        ("--set-llm-credential",),
        ("--rotate-llm-credential",),
        ("--set-tts-credential",),
        ("--rotate-tts-credential",),
        ("--provision-avatar-token",),
        ("--rotate-avatar-token",),
        ("--doctor",),
        ("--doctor-online",),
        ("--doctor-json",),
        ("--doctor-voice-hardware",),
        ("--accept-voice",),
        ("--accept-voice-json",),
        ("--accept-avatar",),
        ("--accept-avatar-json",),
        ("--backup-memory", "backup.db"),
        ("--verify-memory-backup", "backup.db"),
        ("--restore-memory-backup", "backup.db"),
        ("--overwrite-backup",),
    ),
)
def test_desktop_mode_rejects_cli_voice_and_maintenance_modes(
    incompatible_args: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    async_main_called = False

    async def unexpected_async_main(_args: Any) -> int:
        nonlocal async_main_called
        async_main_called = True
        return 0

    monkeypatch.setattr(companion_main, "_configure_cli_streams", lambda: None)
    monkeypatch.setattr(companion_main, "async_main", unexpected_async_main)
    monkeypatch.setattr(sys, "argv", ["companion", "--desktop", *incompatible_args])

    with pytest.raises(SystemExit) as exc_info:
        companion_main.main()

    assert exc_info.value.code == 2
    assert async_main_called is False
