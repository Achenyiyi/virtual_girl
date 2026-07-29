"""Security and lifecycle tests for the AIRI operator launcher."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from companion.airi_launcher import (
    AIRI_PROFILE_MINIMUM_FREE_BYTES,
    AiriProfilePaths,
    build_airi_environment,
    prepare_airi_profile,
    provision_avatar_token,
    run_airi,
    validate_airi_executable,
)
from companion.config_loader import RuntimeConfig
from companion.providers.implementations.websocket_avatar import WebSocketAvatarConfig


def avatar_config() -> RuntimeConfig:
    return RuntimeConfig(
        avatar_config=WebSocketAvatarConfig(
            auth_token_env="COMPANION_AVATAR_TOKEN",
            credential_target="VirtualCompanion/AvatarBridge",
        )
    )


def profile_paths(root: Path) -> AiriProfilePaths:
    return AiriProfilePaths(
        root=root,
        user_data=root / "user-data",
        app_data=root / "appdata",
        local_app_data=root / "local-appdata",
        temp=root / "temp",
    )


def test_child_environment_contains_only_allowlisted_runtime_values_and_avatar_token() -> None:
    profile = profile_paths(Path(r"E:\VirtualCompanion\airi-profile"))
    environment = build_airi_environment(
        "avatar-token",
        profile,
        parent={
            "SystemRoot": r"C:\Windows",
            "Path": r"C:\Windows\System32",
            "TEMP": r"C:\Temp",
            "DEEPSEEK_API_KEY": "llm-secret",
            "AZURE_SPEECH_KEY": "tts-secret",
            "NODE_OPTIONS": "--inspect",
            "ELECTRON_RUN_AS_NODE": "1",
        },
    )

    assert environment == {
        "APPDATA": str(profile.app_data),
        "APP_USER_DATA_PATH": str(profile.user_data),
        "LOCALAPPDATA": str(profile.local_app_data),
        "PATH": r"C:\Windows\System32",
        "SYSTEMROOT": r"C:\Windows",
        "TEMP": str(profile.temp),
        "TMP": str(profile.temp),
        "COMPANION_AVATAR_TOKEN": "avatar-token",
    }


def test_child_environment_requires_a_non_empty_token() -> None:
    with pytest.raises(ValueError, match="token is unavailable"):
        build_airi_environment("", profile_paths(Path(r"E:\airi-profile")))


def test_provisioning_is_idempotent_and_does_not_replace_an_existing_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COMPANION_AVATAR_TOKEN", raising=False)
    monkeypatch.setattr(
        "companion.airi_launcher.write_windows_credential_if_missing",
        lambda _target, _value: False,
    )

    assert provision_avatar_token(avatar_config()) is False


def test_provisioning_creates_a_strong_random_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COMPANION_AVATAR_TOKEN", raising=False)
    written: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "companion.airi_launcher.write_windows_credential_if_missing",
        lambda target, value: written.append((target, value)) or True,
    )

    assert provision_avatar_token(avatar_config()) is True
    assert written[0][0] == "VirtualCompanion/AvatarBridge"
    assert len(written[0][1]) >= 64


def test_provisioning_rejects_a_temporary_environment_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMPANION_AVATAR_TOKEN", "temporary-token")

    with pytest.raises(ValueError, match="Remove the temporary"):
        provision_avatar_token(avatar_config())


def test_executable_validation_rejects_relative_and_non_executable_paths(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="must be absolute"):
        validate_airi_executable(Path("airi.exe"))

    text_file = tmp_path / "airi.txt"
    text_file.write_text("not executable", encoding="ascii")
    with pytest.raises(ValueError, match=r"\.exe"):
        validate_airi_executable(text_file)


def test_profile_preparation_requires_capacity_and_creates_isolated_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "airi-profile"
    checks: list[tuple[Path, int]] = []
    monkeypatch.setattr("companion.airi_launcher.is_remote_path", lambda _path: False)
    monkeypatch.setattr(
        "companion.airi_launcher.check_runtime_storage",
        lambda path, *, minimum_free_bytes: checks.append(
            (Path(path), minimum_free_bytes)
        ),
    )

    paths = prepare_airi_profile(profile)

    assert checks == [
        (profile.resolve() / ".airi-profile-readiness", AIRI_PROFILE_MINIMUM_FREE_BYTES)
    ]
    assert paths == profile_paths(profile.resolve())
    assert all(
        directory.is_dir()
        for directory in (
            paths.root,
            paths.user_data,
            paths.app_data,
            paths.local_app_data,
            paths.temp,
        )
    )


def test_profile_preparation_rejects_relative_remote_and_file_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="must be absolute"):
        prepare_airi_profile(Path("airi-profile"))

    monkeypatch.setattr("companion.airi_launcher.is_remote_path", lambda _path: True)
    with pytest.raises(OSError, match="local Windows volume"):
        prepare_airi_profile(tmp_path / "remote-profile")

    monkeypatch.setattr("companion.airi_launcher.is_remote_path", lambda _path: False)
    file_path = tmp_path / "profile-file"
    file_path.write_bytes(b"not a directory")
    with pytest.raises(ValueError, match="must be a directory"):
        prepare_airi_profile(file_path)


def test_profile_preparation_removes_new_directories_after_partial_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "airi-profile"
    monkeypatch.setattr("companion.airi_launcher.is_remote_path", lambda _path: False)
    monkeypatch.setattr(
        "companion.airi_launcher.check_runtime_storage",
        lambda _path, *, minimum_free_bytes: None,
    )
    real_mkdir = Path.mkdir

    def fail_local_app_data(
        path: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        if path.name == "local-appdata":
            raise OSError("directory creation failed")
        real_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", fail_local_app_data)

    with pytest.raises(OSError, match="directory creation failed"):
        prepare_airi_profile(profile)

    assert not profile.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["companion", "--launch-airi", r"E:\VirtualCompanion\AIRI\AIRI.exe"],
        ["companion", "--airi-profile", r"E:\VirtualCompanion\airi-profile"],
    ],
)
def test_cli_requires_airi_executable_and_profile_together(
    arguments: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from companion.__main__ import main

    monkeypatch.setattr(sys, "argv", arguments)
    monkeypatch.setattr("companion.__main__._configure_cli_streams", lambda: None)

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    assert "--launch-airi 和 --airi-profile 必须一起使用" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_launcher_uses_direct_exec_and_a_sanitized_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "airi.exe"
    executable.write_bytes(b"placeholder")
    monkeypatch.setenv("COMPANION_AVATAR_TOKEN", "avatar-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-leak")
    monkeypatch.setattr(
        "companion.airi_launcher.validate_airi_executable", lambda _path: executable
    )
    profile = profile_paths(tmp_path / "profile")
    monkeypatch.setattr(
        "companion.airi_launcher.prepare_airi_profile", lambda _path: profile
    )
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    class Process:
        async def wait(self) -> int:
            return 17

    async def create_process(*args: str, **kwargs: Any) -> Process:
        calls.append((args, kwargs | {"env": dict(kwargs["env"])}))
        return Process()

    monkeypatch.setattr("companion.airi_launcher.asyncio.create_subprocess_exec", create_process)

    assert await run_airi(avatar_config(), executable, profile.root) == 17
    args, options = calls[0]
    assert args == (str(executable),)
    assert options["cwd"] == str(tmp_path)
    assert options["close_fds"] is True
    assert options["env"]["COMPANION_AVATAR_TOKEN"] == "avatar-token"
    assert options["env"]["APP_USER_DATA_PATH"] == str(profile.user_data)
    assert options["env"]["LOCALAPPDATA"] == str(profile.local_app_data)
    assert "DEEPSEEK_API_KEY" not in options["env"]


@pytest.mark.asyncio
async def test_launcher_terminates_the_child_when_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "airi.exe"
    executable.write_bytes(b"placeholder")
    monkeypatch.setenv("COMPANION_AVATAR_TOKEN", "avatar-token")
    monkeypatch.setattr(
        "companion.airi_launcher.validate_airi_executable", lambda _path: executable
    )
    profile = profile_paths(tmp_path / "profile")
    monkeypatch.setattr(
        "companion.airi_launcher.prepare_airi_profile", lambda _path: profile
    )
    waiting = asyncio.Event()

    class Process:
        def __init__(self) -> None:
            self.terminated = False

        async def wait(self) -> int:
            await waiting.wait()
            return 0

        def terminate(self) -> None:
            self.terminated = True
            waiting.set()

        def kill(self) -> None:
            raise AssertionError("graceful termination should complete")

    process = Process()

    async def create_process(*_args: str, **_kwargs: Any) -> Process:
        return process

    monkeypatch.setattr("companion.airi_launcher.asyncio.create_subprocess_exec", create_process)
    task = asyncio.create_task(run_airi(avatar_config(), executable, profile.root))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.terminated


@pytest.mark.asyncio
async def test_launcher_terminates_the_child_after_a_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "airi.exe"
    executable.write_bytes(b"placeholder")
    monkeypatch.setenv("COMPANION_AVATAR_TOKEN", "avatar-token")
    monkeypatch.setattr(
        "companion.airi_launcher.validate_airi_executable", lambda _path: executable
    )
    profile = profile_paths(tmp_path / "profile")
    monkeypatch.setattr(
        "companion.airi_launcher.prepare_airi_profile", lambda _path: profile
    )

    class Process:
        def __init__(self) -> None:
            self.terminated = False

        async def wait(self) -> int:
            if not self.terminated:
                raise KeyboardInterrupt
            return 0

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            raise AssertionError("graceful termination should complete")

    process = Process()

    async def create_process(*_args: str, **_kwargs: Any) -> Process:
        return process

    monkeypatch.setattr("companion.airi_launcher.asyncio.create_subprocess_exec", create_process)

    with pytest.raises(KeyboardInterrupt):
        await run_airi(avatar_config(), executable, profile.root)
    assert process.terminated
