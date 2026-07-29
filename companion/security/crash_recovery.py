"""Persistent profile marker and SQLite recovery validation after unclean exit."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from companion.memory.memory_service import MemoryService


@dataclass
class CrashRecoveryGuard:
    """Track one runtime generation without storing user content or credentials."""

    memory_path: Path
    marker_path: Path
    run_id: str
    recovered_unclean_exit: bool = False
    _active: bool = False

    @classmethod
    def for_memory_path(cls, memory_path: str) -> CrashRecoveryGuard:
        resolved = Path(memory_path).expanduser().resolve()
        marker = resolved.with_name(f".{resolved.name}.runtime.json")
        return cls(resolved, marker, secrets.token_hex(16))

    def begin(self) -> bool:
        """Validate an unclean profile, then atomically publish this run marker."""
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        self.recovered_unclean_exit = self.marker_path.exists()
        if self.recovered_unclean_exit:
            self._validate_recovery_state()
        payload = json.dumps(
            {
                "schema_version": 1,
                "run_id": self.run_id,
                "pid": os.getpid(),
                "started_at": datetime.now(UTC).isoformat(),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        temporary = self.marker_path.with_name(
            f".{self.marker_path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            os.write(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, self.marker_path)
            self._active = True
        finally:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
        return self.recovered_unclean_exit

    def finish_clean(self) -> None:
        """Remove only the marker that still belongs to this runtime generation."""
        if not self._active:
            return
        try:
            payload = json.loads(self.marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if payload.get("run_id") != self.run_id:
            return
        self.marker_path.unlink()
        self._active = False

    def _validate_recovery_state(self) -> None:
        if not self.memory_path.is_file():
            raise sqlite3.DatabaseError(
                "unclean-exit marker exists but the memory database is missing"
            )
        uri = self.memory_path.as_uri() + "?mode=rw"
        with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
            if not row or row[0] != "ok":
                raise sqlite3.DatabaseError("memory database integrity check failed")
            MemoryService.validate_connection_schema(connection, allow_legacy=True)
