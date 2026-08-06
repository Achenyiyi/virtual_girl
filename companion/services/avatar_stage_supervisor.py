"""Supervise a pinned AIRI desktop process without exposing bridge credentials."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import hashlib
import json
import os
import re
import socket
import struct
import subprocess
import sys
from collections.abc import Awaitable, Callable, Mapping
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_AVATAR_TOKEN_ENV = "COMPANION_AVATAR_TOKEN"
_AVATAR_MODEL_PATH_ENV = "COMPANION_AVATAR_MODEL_PATH"
_AVATAR_MODEL_SHA256_ENV = "COMPANION_AVATAR_MODEL_SHA256"
_AVATAR_MODEL_ID_ENV = "COMPANION_AVATAR_MODEL_ID"
_AVATAR_MODEL_NAME_ENV = "COMPANION_AVATAR_MODEL_NAME"
_CONTROL_URL_ENV = "COMPANION_CONTROL_URL"
_CONTROL_TOKEN_ENV = "COMPANION_CONTROL_TOKEN"
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")
_MODEL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_MAX_MANAGED_VRM_BYTES = 512 * 1024 * 1024
_GLB_HEADER = struct.Struct("<4sII")
_GLB_CHUNK_HEADER = struct.Struct("<I4s")
_GLB_MAGIC = b"glTF"
_GLB_JSON_CHUNK = b"JSON"
_GLB_VERSION = 2
_POLL_INTERVAL_SECONDS = 0.2
_CREATE_SUSPENDED = 0x00000004

# AIRI needs user-profile and Windows runtime locations, but it must not inherit
# unrelated cloud credentials, proxy credentials, or Electron/Node debug hooks.
_CHILD_ENV_ALLOWLIST = frozenset(
    {
        "ALLUSERSPROFILE",
        "APPDATA",
        "COMMONPROGRAMFILES",
        "COMMONPROGRAMFILES(X86)",
        "COMMONPROGRAMW6432",
        "COMPUTERNAME",
        "COMSPEC",
        "DRIVERDATA",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LOCALAPPDATA",
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
        "PROGRAMW6432",
        "PSMODULEPATH",
        "PUBLIC",
        "SESSIONNAME",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERDOMAIN",
        "USERDOMAIN_ROAMINGPROFILE",
        "USERNAME",
        "USERPROFILE",
        "WINDIR",
    }
)


class AvatarStageLaunchError(RuntimeError):
    """The managed AIRI process failed a production launch requirement."""


@dataclass(frozen=True)
class AvatarStageLaunchConfig:
    """Pinned executable and lifecycle settings for a managed AIRI stage."""

    executable_path: str
    expected_sha256: str
    expected_app_asar_sha256: str
    expected_godot_sha256: str
    model_path: str
    expected_model_sha256: str
    model_id: str
    model_name: str = "Managed VRM avatar"
    startup_timeout_seconds: float = 30.0
    shutdown_timeout_seconds: float = 8.0

    def __post_init__(self) -> None:
        if not self.executable_path.strip():
            raise ValueError("managed avatar stage executable_path must not be empty")
        if not _SHA256_PATTERN.fullmatch(self.expected_sha256.strip()):
            raise ValueError(
                "managed avatar stage expected_sha256 must be 64 hexadecimal characters"
            )
        if not _SHA256_PATTERN.fullmatch(self.expected_app_asar_sha256.strip()):
            raise ValueError(
                "managed avatar stage expected_app_asar_sha256 must be 64 hexadecimal "
                "characters"
            )
        if not _SHA256_PATTERN.fullmatch(self.expected_godot_sha256.strip()):
            raise ValueError(
                "managed avatar stage expected_godot_sha256 must be 64 hexadecimal characters"
            )
        if not self.model_path.strip():
            raise ValueError("managed avatar stage model_path must not be empty")
        if not _SHA256_PATTERN.fullmatch(self.expected_model_sha256.strip()):
            raise ValueError(
                "managed avatar stage expected_model_sha256 must be 64 hexadecimal "
                "characters"
            )
        if not _MODEL_ID_PATTERN.fullmatch(self.model_id.strip()):
            raise ValueError(
                "managed avatar stage model_id must use 1-128 ASCII letters, digits, dots, "
                "underscores, or hyphens"
            )
        if not self.model_name.strip() or len(self.model_name.strip()) > 128:
            raise ValueError(
                "managed avatar stage model_name must contain 1-128 characters"
            )
        if not 1.0 <= self.startup_timeout_seconds <= 120.0:
            raise ValueError(
                "managed avatar stage startup timeout must be between 1 and 120 seconds"
            )
        if not 1.0 <= self.shutdown_timeout_seconds <= 30.0:
            raise ValueError(
                "managed avatar stage shutdown timeout must be between 1 and 30 seconds"
            )


class _ManagedProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...


class _ProcessJob(Protocol):
    def assign(self, process_id: int) -> None: ...

    def close(self) -> None: ...


type _ProcessFactory = Callable[..., _ManagedProcess]
type _JobFactory = Callable[[], _ProcessJob]
type _WindowCloser = Callable[[int], bool]
type _EndpointProbe = Callable[[str, int], bool]
type _ThreadResumer = Callable[[_ManagedProcess], None]


class AvatarStageSupervisor:
    """Launch AIRI with a constrained environment and own its process tree."""

    def __init__(
        self,
        config: AvatarStageLaunchConfig,
        *,
        bridge_host: str = "127.0.0.1",
        bridge_port: int = 6122,
        process_factory: _ProcessFactory | None = None,
        job_factory: _JobFactory | None = None,
        window_closer: _WindowCloser | None = None,
        endpoint_probe: _EndpointProbe | None = None,
        thread_resumer: _ThreadResumer | None = None,
        platform: str | None = None,
    ) -> None:
        self._config = config
        self._bridge_host = bridge_host
        self._bridge_port = bridge_port
        self._process_factory = process_factory or subprocess.Popen
        self._job_factory = job_factory or _WindowsKillOnCloseJob
        self._window_closer = window_closer or _request_window_close
        self._endpoint_probe = endpoint_probe or _tcp_endpoint_accepts_connections
        self._thread_resumer = thread_resumer or _resume_suspended_process
        self._platform = platform or sys.platform
        self._process: _ManagedProcess | None = None
        self._job: _ProcessJob | None = None
        self._shutdown_clean = True

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def shutdown_clean(self) -> bool:
        return self._shutdown_clean

    def validate_installation(self) -> tuple[Path, Path]:
        """Validate the pinned AIRI installation and managed VRM asset."""
        if self._platform != "win32":
            raise AvatarStageLaunchError("managed avatar stage is supported only on Windows")
        configured_executable = Path(self._config.executable_path).expanduser()
        executable = configured_executable.resolve()
        if not _same_resolved_path(
            executable, configured_executable.absolute(), platform=self._platform
        ):
            raise AvatarStageLaunchError(
                "managed avatar stage executable must not use a link"
            )
        if executable.suffix.lower() != ".exe" or not executable.is_file():
            raise AvatarStageLaunchError("managed avatar stage executable is unavailable")
        if _is_remote_path(executable, platform=self._platform):
            raise AvatarStageLaunchError("managed avatar stage executable must use a local volume")
        configured_app_asar = executable.parent / "resources" / "app.asar"
        app_asar = configured_app_asar.resolve()
        if app_asar != configured_app_asar:
            raise AvatarStageLaunchError(
                "managed avatar stage application archive must not use a link"
            )
        if not app_asar.is_file() or _is_remote_path(app_asar, platform=self._platform):
            raise AvatarStageLaunchError(
                "managed avatar stage application archive is unavailable"
            )
        configured_godot = (
            executable.parent / "resources" / "godot-stage" / "godot-stage.exe"
        )
        godot = configured_godot.resolve()
        if not _same_resolved_path(
            godot, configured_godot.absolute(), platform=self._platform
        ):
            raise AvatarStageLaunchError(
                "managed avatar stage Godot sidecar must not use a link"
            )
        if godot.suffix.lower() != ".exe" or not godot.is_file():
            raise AvatarStageLaunchError(
                "managed avatar stage Godot sidecar is unavailable"
            )
        if _is_remote_path(godot, platform=self._platform):
            raise AvatarStageLaunchError(
                "managed avatar stage Godot sidecar must use a local volume"
            )
        executable_digest = _sha256_file(
            executable, require_pe=True, label="executable"
        )
        app_asar_digest = _sha256_file(
            app_asar, require_pe=False, label="application archive"
        )
        godot_digest = _sha256_file(godot, require_pe=True, label="Godot sidecar")
        if executable_digest.lower() != self._config.expected_sha256.strip().lower():
            raise AvatarStageLaunchError("managed avatar stage executable digest does not match")
        if (
            app_asar_digest.lower()
            != self._config.expected_app_asar_sha256.strip().lower()
        ):
            raise AvatarStageLaunchError(
                "managed avatar stage application archive digest does not match"
            )
        if godot_digest.lower() != self._config.expected_godot_sha256.strip().lower():
            raise AvatarStageLaunchError(
                "managed avatar stage Godot sidecar digest does not match"
            )
        model = _validate_managed_vrm(
            self._config.model_path,
            self._config.expected_model_sha256,
            platform=self._platform,
        )
        return executable, model

    async def start(
        self,
        token: str,
        *,
        parent_environment: Mapping[str, str] | None = None,
        control_url: str = "",
        control_token: str = "",
    ) -> None:
        """Start the pinned process and attach it to a kill-on-close Windows job."""
        if self.is_running:
            return
        if not token or token != token.strip():
            raise AvatarStageLaunchError(
                "managed avatar stage token is unavailable or malformed"
            )
        await self._cleanup_exited_process()
        executable, model = await asyncio.to_thread(self.validate_installation)
        if await asyncio.to_thread(
            self._endpoint_probe, self._bridge_host, self._bridge_port
        ):
            raise AvatarStageLaunchError(
                "managed avatar stage bridge endpoint is already in use"
            )
        parent = os.environ if parent_environment is None else parent_environment
        environment = _build_child_environment(
            parent,
            token,
            model_path=model,
            model_sha256=self._config.expected_model_sha256,
            model_id=self._config.model_id,
            model_name=self._config.model_name,
            control_url=control_url,
            control_token=control_token,
        )
        creation_flags = (
            int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            | int(getattr(subprocess, "CREATE_DEFAULT_ERROR_MODE", 0))
            | _CREATE_SUSPENDED
        )
        process: _ManagedProcess | None = None
        job: _ProcessJob | None = None
        try:
            job = self._job_factory()
            process = self._process_factory(
                [str(executable)],
                cwd=str(executable.parent),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                close_fds=True,
                creationflags=creation_flags,
            )
            job.assign(process.pid)
            self._thread_resumer(process)
            self._process = process
            self._job = job
            self._shutdown_clean = False
            await asyncio.sleep(0)
            if process.poll() is not None:
                raise AvatarStageLaunchError("managed avatar stage exited during startup")
        except BaseException as exc:
            if process is not None and process.poll() is None:
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    process.terminate()
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    process.wait(timeout=2.0)
            if job is not None:
                job.close()
            self._process = None
            self._job = None
            if isinstance(exc, asyncio.CancelledError) or not isinstance(exc, Exception):
                raise
            if isinstance(exc, AvatarStageLaunchError):
                raise
            raise AvatarStageLaunchError("managed avatar stage process launch failed") from exc

    async def wait_until_ready(self, readiness_check: Callable[[], Awaitable[bool]]) -> None:
        """Poll bridge readiness while also failing on early process exit."""
        process = self._process
        if process is None:
            raise AvatarStageLaunchError("managed avatar stage is not running")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._config.startup_timeout_seconds
        while True:
            if process.poll() is not None:
                raise AvatarStageLaunchError(
                    "managed avatar stage exited before bridge readiness"
                )
            try:
                if await readiness_check():
                    return
            except Exception:
                pass
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AvatarStageLaunchError(
                    "managed avatar stage bridge readiness timed out"
                )
            await asyncio.sleep(min(_POLL_INTERVAL_SECONDS, remaining))

    async def wait_until_bridge_ready(self) -> None:
        """Wait for the configured local bridge port to begin accepting connections."""

        async def bridge_ready() -> bool:
            return await asyncio.to_thread(
                self._endpoint_probe, self._bridge_host, self._bridge_port
            )

        await self.wait_until_ready(bridge_ready)

    async def shutdown(self) -> None:
        """Close AIRI gracefully when possible, then kill any remaining process tree."""
        process, job = self._process, self._job
        self._process = None
        self._job = None
        if process is None:
            if job is not None:
                job.close()
            return
        self._shutdown_clean = await asyncio.to_thread(self._stop_process, process, job)

    async def _cleanup_exited_process(self) -> None:
        if self._process is None or self._process.poll() is None:
            return
        if self._job is not None:
            self._job.close()
        self._process = None
        self._job = None
        self._shutdown_clean = False

    def _stop_process(self, process: _ManagedProcess, job: _ProcessJob | None) -> bool:
        graceful = False
        try:
            if process.poll() is None:
                close_requested = self._window_closer(process.pid)
                if close_requested:
                    try:
                        graceful = (
                            process.wait(
                                timeout=self._config.shutdown_timeout_seconds
                            )
                            == 0
                        )
                    except subprocess.TimeoutExpired:
                        graceful = False
            if process.poll() is None:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.terminate()
                    process.wait(timeout=min(2.0, self._config.shutdown_timeout_seconds))
                graceful = False
        finally:
            if job is not None:
                job.close()
            if process.poll() is None:
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    process.wait(timeout=2.0)
        return graceful


class _ThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


def _resume_suspended_process(process: _ManagedProcess) -> None:
    snapshot_flag = 0x00000004
    thread_suspend_resume = 0x0002
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    create_snapshot.restype = wintypes.HANDLE
    thread_first = kernel32.Thread32First
    thread_first.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    thread_first.restype = wintypes.BOOL
    thread_next = kernel32.Thread32Next
    thread_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    thread_next.restype = wintypes.BOOL
    open_thread = kernel32.OpenThread
    open_thread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_thread.restype = wintypes.HANDLE
    resume_thread = kernel32.ResumeThread
    resume_thread.argtypes = [wintypes.HANDLE]
    resume_thread.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    snapshot = create_snapshot(snapshot_flag, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    resumed = False
    try:
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        has_entry = bool(thread_first(snapshot, ctypes.byref(entry)))
        while has_entry:
            if entry.th32OwnerProcessID == process.pid:
                thread_handle = open_thread(
                    thread_suspend_resume, False, entry.th32ThreadID
                )
                if not thread_handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    if resume_thread(thread_handle) == 0xFFFFFFFF:
                        raise ctypes.WinError(ctypes.get_last_error())
                    resumed = True
                finally:
                    close_handle(thread_handle)
            has_entry = bool(thread_next(snapshot, ctypes.byref(entry)))
    finally:
        close_handle(snapshot)
    if not resumed:
        raise AvatarStageLaunchError("managed avatar stage suspended thread was unavailable")


def _build_child_environment(
    parent: Mapping[str, str],
    token: str,
    *,
    model_path: Path | None = None,
    model_sha256: str = "",
    model_id: str = "",
    model_name: str = "",
    control_url: str = "",
    control_token: str = "",
) -> dict[str, str]:
    if bool(control_url) != bool(control_token):
        raise AvatarStageLaunchError(
            "managed desktop control URL and token must be provided together"
        )
    if control_url and not control_url.startswith("ws://127.0.0.1:"):
        raise AvatarStageLaunchError("managed desktop control URL must use loopback")
    if control_token and control_token != control_token.strip():
        raise AvatarStageLaunchError("managed desktop control token is malformed")
    by_upper = {name.upper(): (name, value) for name, value in parent.items()}
    environment: dict[str, str] = {}
    for allowed in _CHILD_ENV_ALLOWLIST:
        entry = by_upper.get(allowed)
        if entry is not None:
            environment[entry[0]] = entry[1]
    environment[_AVATAR_TOKEN_ENV] = token
    if model_path is not None:
        environment[_AVATAR_MODEL_PATH_ENV] = str(model_path)
        environment[_AVATAR_MODEL_SHA256_ENV] = model_sha256.strip().lower()
        environment[_AVATAR_MODEL_ID_ENV] = model_id.strip()
        environment[_AVATAR_MODEL_NAME_ENV] = model_name.strip()
    if control_url:
        environment[_CONTROL_URL_ENV] = control_url
        environment[_CONTROL_TOKEN_ENV] = control_token
    return environment


def _validate_managed_vrm(
    configured_path: str, expected_sha256: str, *, platform: str
) -> Path:
    configured = Path(configured_path).expanduser()
    model = configured.resolve()
    if not _same_resolved_path(model, configured.absolute(), platform=platform):
        raise AvatarStageLaunchError("managed avatar model must not use a link")
    if model.suffix.lower() != ".vrm" or not model.is_file():
        raise AvatarStageLaunchError("managed avatar model is unavailable")
    if _is_remote_path(model, platform=platform):
        raise AvatarStageLaunchError("managed avatar model must use a local volume")
    try:
        size = model.stat().st_size
    except OSError as exc:
        raise AvatarStageLaunchError("managed avatar model could not be read") from exc
    if not 20 <= size <= _MAX_MANAGED_VRM_BYTES:
        raise AvatarStageLaunchError("managed avatar model size is invalid")
    digest = _sha256_file(model, require_pe=False, label="model")
    if digest.lower() != expected_sha256.strip().lower():
        raise AvatarStageLaunchError("managed avatar model digest does not match")
    _validate_vrm_glb(model, expected_length=size)
    return model


def _validate_vrm_glb(path: Path, *, expected_length: int) -> None:
    try:
        with path.open("rb") as stream:
            header = stream.read(_GLB_HEADER.size)
            if len(header) != _GLB_HEADER.size:
                raise AvatarStageLaunchError("managed avatar model GLB header is invalid")
            magic, version, declared_length = _GLB_HEADER.unpack(header)
            if (
                magic != _GLB_MAGIC
                or version != _GLB_VERSION
                or declared_length != expected_length
            ):
                raise AvatarStageLaunchError("managed avatar model is not a valid GLB 2.0 file")
            chunk_header = stream.read(_GLB_CHUNK_HEADER.size)
            if len(chunk_header) != _GLB_CHUNK_HEADER.size:
                raise AvatarStageLaunchError("managed avatar model JSON chunk is unavailable")
            json_length, chunk_type = _GLB_CHUNK_HEADER.unpack(chunk_header)
            if chunk_type != _GLB_JSON_CHUNK or json_length <= 0:
                raise AvatarStageLaunchError("managed avatar model JSON chunk is invalid")
            json_bytes = stream.read(json_length)
            if len(json_bytes) != json_length:
                raise AvatarStageLaunchError("managed avatar model JSON chunk is truncated")
        document = json.loads(json_bytes.rstrip(b"\x00 \t\r\n").decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AvatarStageLaunchError("managed avatar model metadata could not be read") from exc
    if not isinstance(document, dict) or not isinstance(document.get("asset"), dict):
        raise AvatarStageLaunchError("managed avatar model glTF metadata is invalid")
    extensions = document.get("extensions")
    if not isinstance(extensions, dict) or not any(
        name in extensions for name in ("VRM", "VRMC_vrm")
    ):
        raise AvatarStageLaunchError("managed avatar model does not contain VRM metadata")


def _same_resolved_path(left: Path, right: Path, *, platform: str) -> bool:
    if platform == "win32":
        return os.path.normcase(str(left)) == os.path.normcase(str(right))
    return left == right


def _sha256_file(path: Path, *, require_pe: bool, label: str) -> str:
    try:
        with path.open("rb") as stream:
            if require_pe and stream.read(2) != b"MZ":
                raise AvatarStageLaunchError(
                    f"managed avatar stage {label} is not a PE file"
                )
            stream.seek(0)
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise AvatarStageLaunchError(
            f"managed avatar stage {label} could not be read"
        ) from exc


def _tcp_endpoint_accepts_connections(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def _is_remote_path(path: Path, *, platform: str = sys.platform) -> bool:
    if str(path).startswith("\\\\"):
        return True
    if platform != "win32":
        return False
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    get_drive_type = kernel32.GetDriveTypeW
    get_drive_type.argtypes = [wintypes.LPCWSTR]
    get_drive_type.restype = wintypes.UINT
    return bool(get_drive_type(path.anchor) == 4)


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _WindowsKillOnCloseJob:
    _KILL_ON_JOB_CLOSE = 0x00002000
    _EXTENDED_LIMIT_INFORMATION = 9
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise AvatarStageLaunchError("Windows Job Objects are unavailable")
        self._kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
        self._configure_signatures()
        self._handle = self._kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        information = _JobObjectExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = self._KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            self._handle,
            self._EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign(self, process_id: int) -> None:
        access = self._PROCESS_TERMINATE | self._PROCESS_SET_QUOTA
        process_handle = self._kernel32.OpenProcess(access, False, process_id)
        if not process_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self._kernel32.CloseHandle(process_handle)

    def close(self) -> None:
        handle, self._handle = getattr(self, "_handle", None), None
        if handle:
            self._kernel32.CloseHandle(handle)

    def _configure_signatures(self) -> None:
        self._kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL


def _request_window_close(process_id: int) -> bool:
    if sys.platform != "win32":
        return False
    user32 = ctypes.WinDLL("User32.dll", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    requested = False

    def close_owned_window(window: int, _parameter: int) -> bool:
        nonlocal requested
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == process_id:
            requested = bool(user32.PostMessageW(window, 0x0010, 0, 0)) or requested
        return True

    callback = callback_type(close_owned_window)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.PostMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.PostMessageW.restype = wintypes.BOOL
    if not user32.EnumWindows(callback, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return requested
