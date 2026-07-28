"""Windows session-scoped single-instance guard for the companion runtime."""

from __future__ import annotations

import ctypes
import hashlib
import sys
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

_ERROR_ALREADY_EXISTS = 183


class InstanceAlreadyRunningError(RuntimeError):
    """Another runtime owns the same user-data boundary in this Windows session."""


@dataclass
class SingleInstanceGuard:
    """Own a named Windows mutex until explicit release or process termination."""

    name: str
    _handle: int | None = None

    @classmethod
    def for_memory_path(cls, memory_path: str) -> SingleInstanceGuard:
        resolved = str(Path(memory_path).expanduser().resolve()).casefold()
        digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:32]
        return cls(name=f"Local\\VirtualCompanion-{digest}")

    def acquire(self) -> None:
        if self._handle is not None:
            return
        if sys.platform != "win32":
            raise RuntimeError("single-instance guard requires Windows")
        kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        create_mutex.restype = wintypes.HANDLE
        ctypes.set_last_error(0)
        handle = create_mutex(None, False, self.name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [wintypes.HANDLE]
            close_handle.restype = wintypes.BOOL
            close_handle(handle)
            raise InstanceAlreadyRunningError(
                "Another Virtual Companion runtime is already using this memory store."
            )
        self._handle = int(handle)

    def release(self) -> None:
        if self._handle is None:
            return
        kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        handle = self._handle
        self._handle = None
        if not close_handle(handle):
            raise OSError(ctypes.get_last_error(), "CloseHandle failed")

    def __enter__(self) -> SingleInstanceGuard:
        self.acquire()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.release()
