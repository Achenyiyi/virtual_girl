"""Recovery tests for persistent unclean-runtime markers."""

from __future__ import annotations

import json
import sqlite3
from argparse import Namespace
from types import SimpleNamespace

import pytest

from companion.__main__ import async_main
from companion.config_loader import RuntimeConfig
from companion.memory.memory_service import MemoryService, MemoryServiceConfig
from companion.providers.base import ProviderHealth
from companion.security.crash_recovery import CrashRecoveryGuard


async def _create_memory(path) -> None:
    service = MemoryService(MemoryServiceConfig(db_path=str(path)))
    try:
        await service.health_check()
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_clean_runtime_marker_is_removed(tmp_path) -> None:
    memory_path = tmp_path / "memory.db"
    await _create_memory(memory_path)
    guard = CrashRecoveryGuard.for_memory_path(str(memory_path))

    assert guard.begin() is False
    marker = guard.marker_path
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["run_id"] == guard.run_id
    assert set(payload) == {"schema_version", "run_id", "pid", "started_at"}

    guard.finish_clean()

    assert not marker.exists()


@pytest.mark.asyncio
async def test_leftover_marker_triggers_full_recovery_validation(tmp_path) -> None:
    memory_path = tmp_path / "memory.db"
    await _create_memory(memory_path)
    crashed = CrashRecoveryGuard.for_memory_path(str(memory_path))
    crashed.begin()

    recovered = CrashRecoveryGuard.for_memory_path(str(memory_path))

    assert recovered.begin() is True
    assert recovered.recovered_unclean_exit
    assert json.loads(recovered.marker_path.read_text(encoding="utf-8"))["run_id"] == (
        recovered.run_id
    )
    recovered.finish_clean()


@pytest.mark.asyncio
async def test_unclean_marker_with_corrupt_database_fails_closed(tmp_path) -> None:
    memory_path = tmp_path / "memory.db"
    await _create_memory(memory_path)
    crashed = CrashRecoveryGuard.for_memory_path(str(memory_path))
    crashed.begin()
    memory_path.write_bytes(b"not sqlite")

    recovered = CrashRecoveryGuard.for_memory_path(str(memory_path))

    with pytest.raises(sqlite3.DatabaseError):
        recovered.begin()

    assert recovered.marker_path.exists()


def test_unclean_marker_without_database_fails_closed(tmp_path) -> None:
    guard = CrashRecoveryGuard.for_memory_path(str(tmp_path / "missing.db"))
    guard.marker_path.write_text("{}", encoding="utf-8")

    with pytest.raises(sqlite3.DatabaseError, match="database is missing"):
        guard.begin()


@pytest.mark.asyncio
async def test_stale_runtime_cannot_remove_new_generation_marker(tmp_path) -> None:
    memory_path = tmp_path / "memory.db"
    await _create_memory(memory_path)
    stale = CrashRecoveryGuard.for_memory_path(str(memory_path))
    stale.begin()
    current = CrashRecoveryGuard.for_memory_path(str(memory_path))
    current.begin()

    stale.finish_clean()

    assert current.marker_path.exists()
    current.finish_clean()


@pytest.mark.asyncio
@pytest.mark.parametrize(("shutdown_clean", "marker_removed"), [(True, True), (False, False)])
async def test_runtime_removes_marker_only_after_clean_shutdown(
    tmp_path, monkeypatch, shutdown_clean: bool, marker_removed: bool
) -> None:
    order: list[str] = []

    class FakeMemory:
        async def health_check(self) -> ProviderHealth:
            order.append("memory")
            return ProviderHealth.HEALTHY

    class FakeApp:
        def __init__(self, _config: RuntimeConfig) -> None:
            self.memory = FakeMemory()
            self.shutdown_clean = shutdown_clean
            self.state = SimpleNamespace(identity=SimpleNamespace(name="test"))

        async def start(self) -> bool:
            order.append("start")
            return True

        async def chat(self, _message: str, *, speak: bool) -> dict[str, object]:
            del speak
            return {"response_text": "ok"}

        async def stop(self) -> None:
            order.append("stop")

    class FakeRecoveryGuard:
        def begin(self) -> bool:
            order.append("recovery")
            return False

        def finish_clean(self) -> None:
            order.append("finish")

        @classmethod
        def for_memory_path(cls, _path: str) -> FakeRecoveryGuard:
            return cls()

    class FakeInstanceGuard:
        def acquire(self) -> None:
            order.append("lock")

        def release(self) -> None:
            order.append("unlock")

        @classmethod
        def for_memory_path(cls, _path: str) -> FakeInstanceGuard:
            return cls()

    config = RuntimeConfig(
        memory_config=MemoryServiceConfig(db_path=str(tmp_path / "memory.db"))
    )
    monkeypatch.setattr("companion.__main__.RuntimeConfig.from_yaml", lambda _path: config)
    monkeypatch.setattr("companion.__main__.check_runtime_storage", lambda _path: None)
    monkeypatch.setattr("companion.__main__.SingleInstanceGuard", FakeInstanceGuard)
    monkeypatch.setattr("companion.__main__.CrashRecoveryGuard", FakeRecoveryGuard)
    monkeypatch.setattr("companion.__main__.CompanionApp", FakeApp)
    monkeypatch.setattr("companion.__main__.print_response", lambda *args, **kwargs: None)
    args = Namespace(
        config=None,
        doctor=False,
        doctor_online=False,
        doctor_json=False,
        doctor_voice_hardware=False,
        accept_voice=False,
        accept_voice_json=False,
        accept_avatar=False,
        accept_avatar_json=False,
        backup_memory=None,
        verify_memory_backup=None,
        overwrite_backup=False,
        log_level=None,
        voice_input=False,
        voice=False,
        once="hello",
    )

    assert await async_main(args) == 0

    assert order.index("memory") < order.index("recovery") < order.index("start")
    assert ("finish" in order) is marker_removed
