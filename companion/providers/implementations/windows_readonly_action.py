"""Capability-confined Windows provider for a small set of read-only actions."""

from __future__ import annotations

import asyncio
import ctypes
import os
import platform
import shutil
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from companion.providers.action import (
    ActionPermission,
    ActionProvider,
    ActionRequest,
    ActionResult,
    SandboxStatus,
)
from companion.providers.base import ProviderCapability, ProviderHealth, ProviderInfo

_SUPPORTED_ACTIONS = frozenset(
    {"read_window_title", "read_active_app", "check_system_status"}
)
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


@dataclass(frozen=True)
class WindowsReadOnlyActionConfig:
    """Limits for the immutable Windows read-only capability provider."""

    max_text_characters: int = 4096

    def __post_init__(self) -> None:
        if not 256 <= self.max_text_characters <= 32_768:
            raise ValueError("max_text_characters must be between 256 and 32768")


class WindowsReadOnlyActionProvider(ActionProvider):
    """Expose audited OS facts without shell, input, file, or network mutation."""

    def __init__(self, config: WindowsReadOnlyActionConfig | None = None) -> None:
        self._config = config or WindowsReadOnlyActionConfig()
        self._closed = False
        self._health = ProviderHealth.UNKNOWN
        self._last_health_check = 0.0

    def provider_info(self) -> ProviderInfo:
        return ProviderInfo(
            name="windows-readonly-actions",
            version="1.0.0",
            capabilities=[ProviderCapability.OFFLINE],
            health=self._health,
            last_health_check=self._last_health_check,
        )

    async def health_check(self) -> ProviderHealth:
        self._last_health_check = time.time()
        if self._closed or sys.platform != "win32":
            self._health = ProviderHealth.UNHEALTHY
            return self._health
        try:
            await asyncio.to_thread(_get_foreground_window)
        except OSError:
            self._health = ProviderHealth.UNHEALTHY
        else:
            self._health = ProviderHealth.HEALTHY
        return self._health

    async def execute(self, request: ActionRequest) -> ActionResult:
        started = time.perf_counter()
        try:
            self._validate_request(request, require_sandbox=True)
            if request.action_type == "read_window_title":
                result_data = await asyncio.to_thread(
                    _read_window_title, self._config.max_text_characters
                )
            elif request.action_type == "read_active_app":
                result_data = await asyncio.to_thread(
                    _read_active_app, self._config.max_text_characters
                )
            else:
                result_data = await asyncio.to_thread(_read_system_status)
            return ActionResult(
                action_id=request.action_id,
                success=True,
                method_used=request.method,
                duration_ms=int((time.perf_counter() - started) * 1000),
                result_data=result_data,
                can_undo=False,
            )
        except (OSError, ValueError, PermissionError) as exc:
            return ActionResult(
                action_id=request.action_id,
                success=False,
                method_used=request.method,
                duration_ms=int((time.perf_counter() - started) * 1000),
                error_message=f"{type(exc).__name__}: {exc}",
                can_undo=False,
            )

    async def undo(self, action_id: str) -> ActionResult:
        return ActionResult(
            action_id=action_id,
            success=False,
            method_used="capability_allowlist",
            error_message="Read-only Windows actions have no undo operation",
            can_undo=False,
        )

    async def preview(self, request: ActionRequest) -> dict[str, Any]:
        self._validate_request(request)
        descriptions = {
            "read_window_title": "Read the title of the foreground window",
            "read_active_app": "Read the process name and ID of the foreground application",
            "check_system_status": "Read basic OS, CPU, memory, disk, and uptime status",
        }
        return {
            "action_type": request.action_type,
            "description": descriptions[request.action_type],
            "side_effects": "none",
            "parameters": {},
        }

    async def verify_sandbox(self, request: ActionRequest) -> SandboxStatus:
        try:
            self._validate_request(request)
        except (ValueError, PermissionError) as exc:
            return SandboxStatus(verified=False, reason=str(exc))
        return SandboxStatus(
            verified=True,
            sandbox_id="windows-readonly-allowlist-v1",
            isolation_level="immutable_capability_allowlist",
            reason="Only fixed, parameterless, read-only Win32 queries are implemented",
        )

    async def get_permissions(self) -> list[ActionPermission]:
        return [
            ActionPermission(
                action_pattern=action,
                risk_level="readonly",
                auto_approve=True,
                require_confirmation=False,
                require_user_present=False,
                max_per_hour=120,
            )
            for action in sorted(_SUPPORTED_ACTIONS)
        ]

    async def update_permissions(self, permissions: list[ActionPermission]) -> None:
        del permissions
        raise PermissionError("Windows read-only provider permissions are immutable")

    async def get_audit_log(
        self, limit: int = 100, risk_level: str | None = None
    ) -> list[dict[str, Any]]:
        del limit, risk_level
        return []

    async def shutdown(self) -> None:
        self._closed = True
        self._health = ProviderHealth.UNHEALTHY

    def _validate_request(self, request: ActionRequest, *, require_sandbox: bool = False) -> None:
        if self._closed:
            raise PermissionError("Windows read-only action provider is shut down")
        if sys.platform != "win32":
            raise OSError("Windows read-only actions require Windows")
        if request.action_type not in _SUPPORTED_ACTIONS:
            raise PermissionError(f"Unsupported action: {request.action_type}")
        if str(request.risk_level) != "readonly":
            raise PermissionError("Windows read-only provider accepts only readonly risk")
        if request.parameters:
            raise PermissionError("Windows read-only actions do not accept parameters")
        expected_methods = {
            "read_window_title": "uia",
            "read_active_app": "uia",
            "check_system_status": "api",
        }
        if str(request.method) != expected_methods[request.action_type]:
            raise PermissionError("Action method does not match the fixed provider capability")
        if require_sandbox and request.sandbox_id != "windows-readonly-allowlist-v1":
            raise PermissionError("Action requires this provider's verified sandbox identifier")
        if not require_sandbox and request.sandbox_id not in {
            None,
            "windows-readonly-allowlist-v1",
        }:
            raise PermissionError("Action sandbox identifier does not match this provider")


def _get_foreground_window() -> int:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    window = user32.GetForegroundWindow()
    return int(window or 0)


def _read_window_title(max_characters: int) -> dict[str, Any]:
    window = _get_foreground_window()
    if not window:
        return {"title": "", "window_available": False}
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    buffer = ctypes.create_unicode_buffer(max_characters)
    copied = user32.GetWindowTextW(wintypes.HWND(window), buffer, max_characters)
    if copied == 0 and ctypes.get_last_error():
        raise ctypes.WinError(ctypes.get_last_error())
    return {"title": buffer.value, "window_available": True}


def _read_active_app(max_characters: int) -> dict[str, Any]:
    window = _get_foreground_window()
    if not window:
        return {"pid": 0, "process_name": "", "window_available": False}
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(wintypes.HWND(window), ctypes.byref(process_id))
    if process_id.value == 0:
        raise ctypes.WinError(ctypes.get_last_error())

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    process = kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, process_id.value
    )
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD(max_characters)
        buffer = ctypes.create_unicode_buffer(max_characters)
        if not kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        process_name = Path(buffer.value).name
    finally:
        kernel32.CloseHandle(process)
    return {
        "pid": int(process_id.value),
        "process_name": process_name,
        "window_available": True,
    }


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _read_system_status() -> dict[str, Any]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MemoryStatus)]
    kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
    kernel32.GetTickCount64.restype = ctypes.c_ulonglong
    memory = _MemoryStatus()
    memory.dwLength = ctypes.sizeof(_MemoryStatus)
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
        raise ctypes.WinError(ctypes.get_last_error())
    system_drive = os.environ.get("SYSTEMDRIVE", "C:") + os.sep
    disk = shutil.disk_usage(system_drive)
    return {
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "memory_total_bytes": int(memory.ullTotalPhys),
        "memory_available_bytes": int(memory.ullAvailPhys),
        "memory_load_percent": int(memory.dwMemoryLoad),
        "disk_total_bytes": disk.total,
        "disk_free_bytes": disk.free,
        "uptime_seconds": int(kernel32.GetTickCount64() // 1000),
    }
