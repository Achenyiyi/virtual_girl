"""Credential-safe Windows launcher for the patched AIRI desktop stage."""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from companion.config_loader import RuntimeConfig
from companion.providers.implementations.websocket_avatar import WebSocketAvatarConfig
from companion.security.storage_readiness import check_runtime_storage, is_remote_path
from companion.security.windows_credentials import (
    write_windows_credential_if_missing,
)

_AIRI_TOKEN_ENV = "COMPANION_AVATAR_TOKEN"
_AIRI_USER_DATA_ENV = "APP_USER_DATA_PATH"
AIRI_PROFILE_MINIMUM_FREE_BYTES = 2 * 1024 * 1024 * 1024
_CHILD_ENV_ALLOWLIST = {
    "ALLUSERSPROFILE",
    "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)",
    "COMPUTERNAME",
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOGONSERVER",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "PROCESSOR_LEVEL",
    "PROCESSOR_REVISION",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROMPT",
    "PSMODULEPATH",
    "PUBLIC",
    "SESSIONNAME",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "USERDOMAIN",
    "USERDOMAIN_ROAMINGPROFILE",
    "USERNAME",
    "USERPROFILE",
    "WINDIR",
}


@dataclass(frozen=True)
class AiriProfilePaths:
    """Dedicated writable locations exposed to the AIRI child process."""

    root: Path
    user_data: Path
    app_data: Path
    local_app_data: Path
    temp: Path


def provision_avatar_token(config: RuntimeConfig) -> bool:
    """Create the configured bridge credential only when no credential exists."""
    avatar = _require_airi_avatar_config(config)
    if os.environ.get(_AIRI_TOKEN_ENV, ""):
        raise ValueError(
            "Remove the temporary COMPANION_AVATAR_TOKEN override before provisioning."
        )
    target = avatar.credential_target
    if not target:
        raise ValueError("Avatar token provisioning requires a Windows credential target.")
    token = secrets.token_urlsafe(48)
    try:
        return write_windows_credential_if_missing(target, token)
    finally:
        token = ""


def build_airi_environment(
    token: str,
    profile: AiriProfilePaths,
    *,
    parent: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a minimal child environment that excludes unrelated application secrets."""
    if not token:
        raise ValueError("Avatar bridge token is unavailable from the configured secure source.")
    source = parent if parent is not None else os.environ
    normalized = {key.upper(): value for key, value in source.items()}
    environment = {
        name: normalized[name]
        for name in sorted(_CHILD_ENV_ALLOWLIST)
        if normalized.get(name)
    }
    environment.update(
        {
            _AIRI_USER_DATA_ENV: str(profile.user_data),
            "APPDATA": str(profile.app_data),
            "LOCALAPPDATA": str(profile.local_app_data),
            "TEMP": str(profile.temp),
            "TMP": str(profile.temp),
        }
    )
    environment[_AIRI_TOKEN_ENV] = token
    return environment


def validate_airi_executable(executable: Path | str) -> Path:
    """Require an existing local absolute Windows executable."""
    raw = Path(executable).expanduser()
    if not raw.is_absolute():
        raise ValueError("AIRI executable path must be absolute.")
    resolved = raw.resolve(strict=True)
    if sys.platform != "win32":
        raise OSError("The AIRI launcher is supported only on Windows.")
    if not resolved.is_file() or resolved.suffix.lower() != ".exe":
        raise ValueError("AIRI executable must be an existing .exe file.")
    if is_remote_path(resolved):
        raise OSError("AIRI executable must be on a local Windows volume.")
    return resolved


def prepare_airi_profile(profile: Path | str) -> AiriProfilePaths:
    """Create an isolated local AIRI profile after proving capacity and writability."""
    raw = Path(profile).expanduser()
    if not raw.is_absolute():
        raise ValueError("AIRI profile path must be absolute.")
    resolved = raw.resolve()
    if sys.platform != "win32":
        raise OSError("The AIRI launcher is supported only on Windows.")
    if is_remote_path(resolved):
        raise OSError("AIRI profile must be on a local Windows volume.")
    if resolved == Path(resolved.anchor):
        raise ValueError("AIRI profile must be a dedicated directory, not a volume root.")
    if resolved.exists() and not resolved.is_dir():
        raise ValueError("AIRI profile path must be a directory.")

    paths = AiriProfilePaths(
        root=resolved,
        user_data=resolved / "user-data",
        app_data=resolved / "appdata",
        local_app_data=resolved / "local-appdata",
        temp=resolved / "temp",
    )
    check_runtime_storage(
        resolved / ".airi-profile-readiness",
        minimum_free_bytes=AIRI_PROFILE_MINIMUM_FREE_BYTES,
    )
    directories = (
        paths.root,
        paths.user_data,
        paths.app_data,
        paths.local_app_data,
        paths.temp,
    )
    for directory in directories:
        if directory.exists() and not directory.is_dir():
            raise ValueError(f"AIRI profile location must be a directory: {directory}")
    created_directories: list[Path] = []
    try:
        for directory in directories:
            if not directory.exists():
                directory.mkdir(parents=True, exist_ok=False)
                created_directories.append(directory)
    except BaseException:
        for directory in reversed(created_directories):
            with contextlib.suppress(OSError):
                directory.rmdir()
        raise
    return paths


async def run_airi(
    config: RuntimeConfig,
    executable: Path | str,
    profile: Path | str,
) -> int:
    """Run AIRI in the foreground and clean it up if the operator interrupts the launcher."""
    avatar = _require_airi_avatar_config(config)
    airi_executable = validate_airi_executable(executable)
    profile_paths = prepare_airi_profile(profile)
    token = avatar.get_auth_token()
    environment = build_airi_environment(token, profile_paths)
    try:
        process = await asyncio.create_subprocess_exec(
            str(airi_executable),
            cwd=str(airi_executable.parent),
            env=environment,
            close_fds=True,
        )
    finally:
        environment.pop(_AIRI_TOKEN_ENV, None)
        token = ""
    try:
        return await process.wait()
    except asyncio.CancelledError:
        await _terminate_process(process)
        raise
    except KeyboardInterrupt:
        await _terminate_process(process)
        raise


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """Stop AIRI gracefully, then force termination after a bounded wait."""
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(asyncio.shield(process.wait()), timeout=5.0)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()


def _require_airi_avatar_config(config: RuntimeConfig) -> WebSocketAvatarConfig:
    avatar = config.avatar_config
    if avatar is None:
        raise ValueError("Avatar bridge must be enabled in the selected configuration.")
    if avatar.auth_token_env != _AIRI_TOKEN_ENV:
        raise ValueError(
            "The AIRI v0.11.3 bridge requires auth_token_env COMPANION_AVATAR_TOKEN."
        )
    return avatar
