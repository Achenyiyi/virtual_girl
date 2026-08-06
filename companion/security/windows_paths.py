"""Windows path helpers shared across the runtime."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

_DRIVE_REMOTE = 4


def is_remote_path(path: Path, *, platform: str = sys.platform) -> bool:
    """True when a path lives on a network share (UNC prefix or remote drive)."""
    if str(path).startswith("\\\\"):
        return True
    if platform != "win32":
        return False
    anchor = path.anchor
    if not anchor:
        return False
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    get_drive_type = kernel32.GetDriveTypeW
    get_drive_type.argtypes = [wintypes.LPCWSTR]
    get_drive_type.restype = wintypes.UINT
    return bool(get_drive_type(anchor) == _DRIVE_REMOTE)
