"""Fail-closed checks for writable, local, adequately sized runtime storage."""

from __future__ import annotations

import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

from companion.security.windows_paths import is_remote_path as _is_remote_path

MINIMUM_RUNTIME_FREE_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class StorageReadiness:
    path: Path
    parent: Path
    free_bytes: int


def check_runtime_storage(
    path: str | Path,
    *,
    minimum_free_bytes: int = MINIMUM_RUNTIME_FREE_BYTES,
) -> StorageReadiness:
    """Prove a local runtime path can be written and has enough free space."""
    if minimum_free_bytes <= 0:
        raise ValueError("minimum runtime free space must be positive")
    resolved = Path(path).expanduser().resolve()
    existing_parent = _nearest_existing_parent(resolved.parent)
    if not existing_parent.is_dir():
        raise OSError("runtime path has no existing parent directory")
    if _is_remote_path(resolved):
        raise OSError("runtime storage must use a local Windows volume")
    free_bytes = shutil.disk_usage(existing_parent).free
    if free_bytes < minimum_free_bytes:
        raise OSError("runtime storage has insufficient free space")
    created_directories = _create_missing_parents(resolved.parent, existing_parent)
    try:
        _probe_write(resolved.parent)
        if resolved.exists():
            descriptor = os.open(resolved, os.O_RDWR)
            os.close(descriptor)
    finally:
        for directory in reversed(created_directories):
            directory.rmdir()
    return StorageReadiness(resolved, resolved.parent, free_bytes)


def _probe_write(parent: Path) -> None:
    probe = parent / f".virtual-companion-write-{os.getpid()}-{secrets.token_hex(8)}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(descriptor, b"storage-ready")
        os.fsync(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        probe.unlink(missing_ok=True)


def _create_missing_parents(parent: Path, existing_parent: Path) -> list[Path]:
    missing: list[Path] = []
    current = parent
    while current != existing_parent:
        missing.append(current)
        current = current.parent
    created: list[Path] = []
    try:
        for directory in reversed(missing):
            directory.mkdir()
            created.append(directory)
    except BaseException:
        for directory in reversed(created):
            directory.rmdir()
        raise
    return created


def _nearest_existing_parent(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current
