"""Security and lifecycle tests for managed AIRI process supervision."""

from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from companion.services.avatar_stage_supervisor import (
    AvatarStageLaunchConfig,
    AvatarStageLaunchError,
    AvatarStageSupervisor,
    _build_child_environment,
)


class FakeProcess:
    pid = 4321

    def __init__(self, *, running: bool = True) -> None:
        self.return_code = None if running else 1
        self.terminated = False
        self.wait_timeouts: list[float | None] = []

    def poll(self) -> int | None:
        return self.return_code

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if self.return_code is None:
            raise subprocess.TimeoutExpired("airi.exe", timeout or 0.0)
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True
        self.return_code = 1


class FakeJob:
    def __init__(self, *, fail_assign: bool = False) -> None:
        self.fail_assign = fail_assign
        self.assigned: list[int] = []
        self.closed = False

    def assign(self, process_id: int) -> None:
        self.assigned.append(process_id)
        if self.fail_assign:
            raise OSError("job assignment failed")

    def close(self) -> None:
        self.closed = True


def _write_vrm(path: Path) -> str:
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
    return hashlib.sha256(content).hexdigest()


def _write_installation(path: Path) -> tuple[str, str, str, Path, str]:
    content = b"MZ" + b"approved-airi-binary"
    path.write_bytes(content)
    app_asar = path.parent / "resources" / "app.asar"
    app_asar.parent.mkdir()
    archive_content = b"approved-airi-application"
    app_asar.write_bytes(archive_content)
    godot = path.parent / "resources" / "godot-stage" / "godot-stage.exe"
    godot.parent.mkdir()
    godot_content = b"MZ" + b"approved-godot-sidecar"
    godot.write_bytes(godot_content)
    model = path.parent / "avatar.vrm"
    model_digest = _write_vrm(model)
    return (
        hashlib.sha256(content).hexdigest(),
        hashlib.sha256(archive_content).hexdigest(),
        hashlib.sha256(godot_content).hexdigest(),
        model,
        model_digest,
    )


def _supervisor(
    executable: Path,
    digest: str,
    app_asar_digest: str,
    godot_digest: str,
    model: Path,
    model_digest: str,
    **kwargs: Any,
) -> AvatarStageSupervisor:
    return AvatarStageSupervisor(
        AvatarStageLaunchConfig(
            str(executable),
            digest,
            app_asar_digest,
            godot_digest,
            str(model),
            model_digest,
            "managed-nemesia",
            "Nemesia pajamas",
        ),
        platform="win32",
        endpoint_probe=lambda _host, _port: False,
        **kwargs,
    )


def test_validate_installation_requires_pinned_local_pe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "airi.exe"
    digest, app_asar_digest, godot_digest, model, model_digest = _write_installation(
        executable
    )
    supervisor = _supervisor(
        executable, digest, app_asar_digest, godot_digest, model, model_digest
    )
    monkeypatch.setattr(
        "companion.services.avatar_stage_supervisor._is_remote_path",
        lambda _path, *, platform: False,
    )

    assert supervisor.validate_installation() == (
        executable.resolve(),
        model.resolve(),
    )

    executable.write_bytes(b"MZmodified")
    with pytest.raises(AvatarStageLaunchError, match="digest"):
        supervisor.validate_installation()


def test_validate_installation_rejects_remote_and_non_windows_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "airi.exe"
    digest, app_asar_digest, godot_digest, model, model_digest = _write_installation(
        executable
    )
    monkeypatch.setattr(
        "companion.services.avatar_stage_supervisor._is_remote_path",
        lambda _path, *, platform: True,
    )

    with pytest.raises(AvatarStageLaunchError, match="local volume"):
        _supervisor(
            executable, digest, app_asar_digest, godot_digest, model, model_digest
        ).validate_installation()
    with pytest.raises(AvatarStageLaunchError, match="only on Windows"):
        AvatarStageSupervisor(
            AvatarStageLaunchConfig(
                str(executable),
                digest,
                app_asar_digest,
                godot_digest,
                str(model),
                model_digest,
                "managed-nemesia",
            ),
            platform="linux",
        ).validate_installation()


def test_validate_installation_rejects_modified_godot_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "airi.exe"
    digest, app_asar_digest, godot_digest, model, model_digest = _write_installation(
        executable
    )
    supervisor = _supervisor(
        executable, digest, app_asar_digest, godot_digest, model, model_digest
    )
    monkeypatch.setattr(
        "companion.services.avatar_stage_supervisor._is_remote_path",
        lambda _path, *, platform: False,
    )
    godot = executable.parent / "resources" / "godot-stage" / "godot-stage.exe"
    godot.write_bytes(b"MZmodified-godot-sidecar")

    with pytest.raises(AvatarStageLaunchError, match="Godot sidecar digest"):
        supervisor.validate_installation()


def test_child_environment_excludes_credentials_proxy_and_debug_hooks() -> None:
    environment = _build_child_environment(
        {
            "PATH": "C:\\Windows",
            "USERPROFILE": "C:\\Users\\operator",
            "DEEPSEEK_API_KEY": "must-not-leak",
            "AZURE_SPEECH_KEY": "must-not-leak",
            "HTTPS_PROXY": "must-not-leak",
            "NODE_OPTIONS": "must-not-leak",
            "ELECTRON_RUN_AS_NODE": "must-not-leak",
            "COMPANION_AVATAR_TOKEN": "old-token",
        },
        "bridge-token",
    )

    assert environment == {
        "PATH": "C:\\Windows",
        "USERPROFILE": "C:\\Users\\operator",
        "COMPANION_AVATAR_TOKEN": "bridge-token",
    }


def test_validate_installation_rejects_modified_or_non_vrm_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "airi.exe"
    digest, app_asar_digest, godot_digest, model, model_digest = _write_installation(
        executable
    )
    supervisor = _supervisor(
        executable, digest, app_asar_digest, godot_digest, model, model_digest
    )
    monkeypatch.setattr(
        "companion.services.avatar_stage_supervisor._is_remote_path",
        lambda _path, *, platform: False,
    )

    original_size = model.stat().st_size
    model.write_bytes(b"x" * original_size)
    with pytest.raises(AvatarStageLaunchError, match="digest"):
        supervisor.validate_installation()

    invalid_digest = hashlib.sha256(b"x" * original_size).hexdigest()
    invalid = _supervisor(
        executable, digest, app_asar_digest, godot_digest, model, invalid_digest
    )
    with pytest.raises(AvatarStageLaunchError, match="GLB|size"):
        invalid.validate_installation()


@pytest.mark.asyncio
async def test_start_uses_exact_process_boundary_and_job_assignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "airi.exe"
    digest, app_asar_digest, godot_digest, model, model_digest = _write_installation(
        executable
    )
    process = FakeProcess()
    job = FakeJob()
    calls: list[dict[str, Any]] = []
    resumed: list[int] = []

    def process_factory(*args: Any, **kwargs: Any) -> FakeProcess:
        calls.append({"args": args, "kwargs": kwargs})
        return process

    monkeypatch.setattr(
        "companion.services.avatar_stage_supervisor._is_remote_path",
        lambda _path, *, platform: False,
    )
    supervisor = _supervisor(
        executable,
        digest,
        app_asar_digest,
        godot_digest,
        model,
        model_digest,
        process_factory=process_factory,
        job_factory=lambda: job,
        thread_resumer=lambda child: resumed.append(child.pid),
    )

    await supervisor.start(
        "bridge-token",
        parent_environment={"PATH": "C:\\Windows", "DEEPSEEK_API_KEY": "secret"},
    )

    assert job.assigned == [process.pid]
    assert resumed == [process.pid]
    assert calls[0]["args"] == ([str(executable.resolve())],)
    options = calls[0]["kwargs"]
    assert options["cwd"] == str(tmp_path.resolve())
    assert options["shell"] is False
    assert options["stdin"] is subprocess.DEVNULL
    assert options["stdout"] is subprocess.DEVNULL
    assert options["stderr"] is subprocess.DEVNULL
    assert options["env"] == {
        "PATH": "C:\\Windows",
        "COMPANION_AVATAR_TOKEN": "bridge-token",
        "COMPANION_AVATAR_MODEL_PATH": str(model.resolve()),
        "COMPANION_AVATAR_MODEL_SHA256": model_digest,
        "COMPANION_AVATAR_MODEL_ID": "managed-nemesia",
        "COMPANION_AVATAR_MODEL_NAME": "Nemesia pajamas",
    }
    assert options["creationflags"] & 0x00000004


@pytest.mark.asyncio
async def test_start_refuses_preexisting_bridge_without_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "airi.exe"
    digest, app_asar_digest, godot_digest, model, model_digest = _write_installation(
        executable
    )
    monkeypatch.setattr(
        "companion.services.avatar_stage_supervisor._is_remote_path",
        lambda _path, *, platform: False,
    )
    spawned = False

    def process_factory(*_args: Any, **_kwargs: Any) -> FakeProcess:
        nonlocal spawned
        spawned = True
        return FakeProcess()

    supervisor = AvatarStageSupervisor(
        AvatarStageLaunchConfig(
            str(executable),
            digest,
            app_asar_digest,
            godot_digest,
            str(model),
            model_digest,
            "managed-nemesia",
        ),
        platform="win32",
        endpoint_probe=lambda _host, _port: True,
        process_factory=process_factory,
    )

    with pytest.raises(AvatarStageLaunchError, match="already in use"):
        await supervisor.start("bridge-token")
    assert not spawned


@pytest.mark.asyncio
async def test_job_assignment_failure_terminates_process_and_closes_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "airi.exe"
    digest, app_asar_digest, godot_digest, model, model_digest = _write_installation(
        executable
    )
    process = FakeProcess()
    job = FakeJob(fail_assign=True)
    monkeypatch.setattr(
        "companion.services.avatar_stage_supervisor._is_remote_path",
        lambda _path, *, platform: False,
    )
    supervisor = _supervisor(
        executable,
        digest,
        app_asar_digest,
        godot_digest,
        model,
        model_digest,
        process_factory=lambda *_args, **_kwargs: process,
        job_factory=lambda: job,
        thread_resumer=lambda _process: None,
    )

    with pytest.raises(AvatarStageLaunchError, match="process launch failed"):
        await supervisor.start("bridge-token")

    assert process.terminated
    assert job.closed
    assert not supervisor.is_running


@pytest.mark.asyncio
async def test_readiness_fails_on_early_exit_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AvatarStageLaunchConfig(
        "airi.exe",
        "a" * 64,
        "b" * 64,
        "d" * 64,
        "avatar.vrm",
        "c" * 64,
        "managed-nemesia",
        startup_timeout_seconds=1.0,
    )
    process = FakeProcess(running=False)
    supervisor = AvatarStageSupervisor(config, platform="win32")
    supervisor._process = process

    with pytest.raises(AvatarStageLaunchError, match="exited before"):
        await supervisor.wait_until_ready(lambda: asyncio.sleep(0, result=False))

    process.return_code = None
    times = iter([0.0, 2.0])
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: FakeLoop(times))
    with pytest.raises(AvatarStageLaunchError, match="timed out"):
        await supervisor.wait_until_ready(lambda: asyncio.sleep(0, result=False))


class FakeLoop:
    def __init__(self, times: Iterator[float]) -> None:
        self._times = times

    def time(self) -> float:
        return next(self._times)


@pytest.mark.asyncio
async def test_shutdown_requests_window_close_then_forces_and_closes_job() -> None:
    process = FakeProcess()
    job = FakeJob()
    supervisor = AvatarStageSupervisor(
        AvatarStageLaunchConfig(
            "airi.exe",
            "a" * 64,
            "b" * 64,
            "d" * 64,
            "avatar.vrm",
            "c" * 64,
            "model",
        ),
        platform="win32",
        window_closer=lambda _pid: False,
    )
    supervisor._process = process
    supervisor._job = job

    await supervisor.shutdown()

    assert process.terminated
    assert job.closed
    assert not supervisor.shutdown_clean


@pytest.mark.asyncio
async def test_already_exited_process_is_not_reported_as_clean_shutdown() -> None:
    process = FakeProcess(running=False)
    job = FakeJob()
    supervisor = AvatarStageSupervisor(
        AvatarStageLaunchConfig(
            "airi.exe",
            "a" * 64,
            "b" * 64,
            "d" * 64,
            "avatar.vrm",
            "c" * 64,
            "model",
        ),
        platform="win32",
    )
    supervisor._process = process
    supervisor._job = job

    await supervisor.shutdown()

    assert job.closed
    assert not supervisor.shutdown_clean
