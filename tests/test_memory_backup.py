from __future__ import annotations

import asyncio
import sqlite3
import threading
from argparse import Namespace

import pytest

from companion.__main__ import async_main
from companion.memory.memory_service import MemoryService, MemoryServiceConfig
from companion.security.single_instance import SingleInstanceGuard


@pytest.mark.asyncio
async def test_memory_creates_missing_parent_and_verified_backup(tmp_path) -> None:
    live_path = tmp_path / "runtime" / "nested" / "memory.db"
    backup_path = tmp_path / "backups" / "memory.db"
    memory = MemoryService(MemoryServiceConfig(db_path=str(live_path)))
    try:
        await memory.append_event(
            {"event_id": "evt_backup", "event_type": "conversation.turn.completed"}
        )
        created = await memory.backup_to(backup_path)
    finally:
        await memory.shutdown()

    assert created == backup_path.resolve()
    assert live_path.is_file()
    MemoryService.verify_backup(backup_path)
    with sqlite3.connect(backup_path) as connection:
        assert connection.execute("SELECT event_id FROM events").fetchone() == ("evt_backup",)


@pytest.mark.asyncio
async def test_backup_refuses_overwrite_without_explicit_opt_in(tmp_path) -> None:
    memory = MemoryService(MemoryServiceConfig(db_path=str(tmp_path / "memory.db")))
    backup = tmp_path / "backup.db"
    try:
        await memory.backup_to(backup)
        with pytest.raises(FileExistsError):
            await memory.backup_to(backup)
        await memory.append_event({"event_id": "evt_new", "event_type": "test.event"})
        await memory.backup_to(backup, overwrite=True)
    finally:
        await memory.shutdown()

    with sqlite3.connect(backup) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_id = 'evt_new'"
        ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_backup_and_write_are_serialized(tmp_path, monkeypatch) -> None:
    memory = MemoryService(MemoryServiceConfig(db_path=str(tmp_path / "memory.db")))
    backup = tmp_path / "backup.db"
    entered_backup = threading.Event()
    release_backup = threading.Event()
    original_backup = memory._backup_sqlite_file

    def blocked_backup(source, destination) -> None:
        entered_backup.set()
        assert release_backup.wait(timeout=10)
        original_backup(source, destination)

    monkeypatch.setattr(memory, "_backup_sqlite_file", blocked_backup)
    try:
        backup_task = asyncio.create_task(memory.backup_to(backup))
        assert await asyncio.to_thread(entered_backup.wait, 10)
        write_task = asyncio.create_task(
            memory.append_event({"event_id": "evt_after_snapshot", "event_type": "test.event"})
        )
        await asyncio.sleep(0)
        assert not write_task.done()

        release_backup.set()
        await backup_task
        await write_task
    finally:
        await memory.shutdown()

    with sqlite3.connect(backup) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_id = 'evt_after_snapshot'"
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_cancelled_backup_finishes_worker_before_releasing_database(tmp_path, monkeypatch):
    memory = MemoryService(MemoryServiceConfig(db_path=str(tmp_path / "memory.db")))
    backup = tmp_path / "cancelled.db"
    entered_backup = threading.Event()
    release_backup = threading.Event()
    original_backup = memory._backup_sqlite_file

    def blocked_backup(source, destination) -> None:
        entered_backup.set()
        assert release_backup.wait(timeout=10)
        original_backup(source, destination)

    monkeypatch.setattr(memory, "_backup_sqlite_file", blocked_backup)
    try:
        backup_task = asyncio.create_task(memory.backup_to(backup))
        assert await asyncio.to_thread(entered_backup.wait, 10)
        backup_task.cancel()
        write_task = asyncio.create_task(
            memory.append_event({"event_id": "evt_after_cancel", "event_type": "test.event"})
        )
        await asyncio.sleep(0)
        assert not backup_task.done()
        assert not write_task.done()

        release_backup.set()
        with pytest.raises(asyncio.CancelledError):
            await backup_task
        assert await write_task == "evt_after_cancel"
    finally:
        await memory.shutdown()

    assert not backup.exists()
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.asyncio
async def test_backup_does_not_overwrite_target_created_during_verification(
    tmp_path, monkeypatch
) -> None:
    memory = MemoryService(MemoryServiceConfig(db_path=str(tmp_path / "memory.db")))
    target = tmp_path / "raced.db"
    original_verify = memory.verify_backup

    def create_competing_target(path) -> None:
        original_verify(path)
        target.write_bytes(b"competing backup")

    monkeypatch.setattr(memory, "verify_backup", create_competing_target)
    try:
        with pytest.raises(FileExistsError):
            await memory.backup_to(target)
    finally:
        await memory.shutdown()

    assert target.read_bytes() == b"competing backup"
    assert not list(tmp_path.glob(".*.tmp"))


def test_verify_backup_rejects_corrupt_or_unrelated_sqlite(tmp_path) -> None:
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        MemoryService.verify_backup(corrupt)

    unrelated = tmp_path / "unrelated.db"
    with sqlite3.connect(unrelated) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
    with pytest.raises(sqlite3.DatabaseError, match="ownership marker"):
        MemoryService.verify_backup(unrelated)


@pytest.mark.asyncio
async def test_restore_replaces_memory_and_preserves_previous_files(tmp_path) -> None:
    live_path = tmp_path / "memory.db"
    backup_path = tmp_path / "backup.db"
    memory = MemoryService(MemoryServiceConfig(db_path=str(live_path)))
    try:
        await memory.append_event({"event_id": "evt_old", "event_type": "test.event"})
        await memory.backup_to(backup_path)
        await memory.append_event({"event_id": "evt_new", "event_type": "test.event"})
    finally:
        await memory.shutdown()
    restored, rollback_dir = MemoryService.restore_from_backup(backup_path, live_path)

    assert restored == live_path.resolve()
    assert rollback_dir is not None and rollback_dir.is_dir()
    with sqlite3.connect(live_path) as connection:
        assert connection.execute("SELECT event_id FROM events ORDER BY event_id").fetchall() == [
            ("evt_old",)
        ]
    with sqlite3.connect(rollback_dir / live_path.name) as connection:
        assert connection.execute("SELECT event_id FROM events ORDER BY event_id").fetchall() == [
            ("evt_new",),
            ("evt_old",),
        ]


@pytest.mark.asyncio
async def test_invalid_restore_does_not_change_live_memory(tmp_path) -> None:
    live_path = tmp_path / "memory.db"
    memory = MemoryService(MemoryServiceConfig(db_path=str(live_path)))
    try:
        await memory.append_event({"event_id": "evt_live", "event_type": "test.event"})
    finally:
        await memory.shutdown()
    invalid = tmp_path / "invalid.db"
    invalid.write_bytes(b"not sqlite")

    with pytest.raises(sqlite3.DatabaseError):
        MemoryService.restore_from_backup(invalid, live_path)

    with sqlite3.connect(live_path) as connection:
        assert connection.execute("SELECT event_id FROM events").fetchone() == ("evt_live",)
    assert not list(tmp_path.glob(".memory.db.pre-restore-*"))


@pytest.mark.asyncio
async def test_failed_post_replace_validation_restores_previous_memory(
    tmp_path, monkeypatch
) -> None:
    live_path = tmp_path / "memory.db"
    backup_path = tmp_path / "backup.db"
    memory = MemoryService(MemoryServiceConfig(db_path=str(live_path)))
    try:
        await memory.append_event({"event_id": "evt_old", "event_type": "test.event"})
        await memory.backup_to(backup_path)
        await memory.append_event({"event_id": "evt_live", "event_type": "test.event"})
    finally:
        await memory.shutdown()
    original_verify = MemoryService.verify_backup
    calls = 0

    def fail_final_validation(path):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise sqlite3.DatabaseError("injected final validation failure")
        return original_verify(path)

    monkeypatch.setattr(MemoryService, "verify_backup", fail_final_validation)

    with pytest.raises(sqlite3.DatabaseError, match="injected"):
        MemoryService.restore_from_backup(backup_path, live_path)

    with sqlite3.connect(live_path) as connection:
        assert connection.execute("SELECT event_id FROM events ORDER BY event_id").fetchall() == [
            ("evt_live",),
            ("evt_old",),
        ]


@pytest.mark.asyncio
async def test_failed_new_restore_removes_unpublished_destination(tmp_path, monkeypatch) -> None:
    backup_path = tmp_path / "backup.db"
    destination = tmp_path / "new-memory.db"
    orphan_wal = tmp_path / "new-memory.db-wal"
    orphan_wal.write_bytes(b"orphan wal")
    memory = MemoryService(MemoryServiceConfig(db_path=str(tmp_path / "source.db")))
    try:
        await memory.backup_to(backup_path)
    finally:
        await memory.shutdown()
    original_verify = MemoryService.verify_backup
    calls = 0

    def fail_final_validation(path):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise sqlite3.DatabaseError("injected final validation failure")
        return original_verify(path)

    monkeypatch.setattr(MemoryService, "verify_backup", fail_final_validation)

    with pytest.raises(sqlite3.DatabaseError, match="injected"):
        MemoryService.restore_from_backup(backup_path, destination)

    assert not destination.exists()
    assert orphan_wal.read_bytes() == b"orphan wal"


@pytest.mark.asyncio
async def test_backup_rejects_live_database_as_destination(tmp_path) -> None:
    live = tmp_path / "memory.db"
    memory = MemoryService(MemoryServiceConfig(db_path=str(live)))
    try:
        with pytest.raises(ValueError, match="must differ"):
            await memory.backup_to(live)
    finally:
        await memory.shutdown()


@pytest.mark.asyncio
async def test_in_memory_database_cannot_be_exported_as_release_backup(tmp_path) -> None:
    memory = MemoryService(MemoryServiceConfig(db_path=":memory:"))
    try:
        with pytest.raises(ValueError, match="in-memory"):
            await memory.backup_to(tmp_path / "backup.db")
    finally:
        await memory.shutdown()


@pytest.mark.asyncio
async def test_cli_backup_and_config_independent_verification(tmp_path) -> None:
    config_path = tmp_path / "companion.yaml"
    live_path = tmp_path / "runtime" / "memory.db"
    backup_path = tmp_path / "backup" / "memory.db"
    config_path.write_text(
        f"""providers:
  memory:
    type: sqlite
    db_path: {live_path.as_posix()}
""",
        encoding="utf-8",
    )
    base = {
        "config": config_path,
        "doctor": False,
        "doctor_online": False,
        "doctor_json": False,
        "doctor_voice_hardware": False,
        "voice_input": False,
        "voice": False,
        "overwrite_backup": False,
    }

    assert (
        await async_main(
            Namespace(
                **base,
                backup_memory=backup_path,
                verify_memory_backup=None,
            )
        )
        == 0
    )

    config_path.unlink()
    assert (
        await async_main(
            Namespace(
                **base,
                backup_memory=None,
                verify_memory_backup=backup_path,
            )
        )
        == 0
    )


@pytest.mark.asyncio
async def test_cli_restore_replaces_configured_memory(tmp_path) -> None:
    config_path = tmp_path / "companion.yaml"
    live_path = tmp_path / "runtime" / "memory.db"
    backup_path = tmp_path / "backup.db"
    config_path.write_text(
        f"""providers:
  memory:
    type: sqlite
    db_path: {live_path.as_posix()}
""",
        encoding="utf-8",
    )
    memory = MemoryService(MemoryServiceConfig(db_path=str(live_path)))
    try:
        await memory.append_event({"event_id": "evt_backup", "event_type": "test.event"})
        await memory.backup_to(backup_path)
        await memory.append_event({"event_id": "evt_later", "event_type": "test.event"})
    finally:
        await memory.shutdown()

    args = Namespace(
        config=config_path,
        doctor=False,
        doctor_online=False,
        doctor_json=False,
        doctor_voice_hardware=False,
        voice_input=False,
        voice=False,
        backup_memory=None,
        verify_memory_backup=None,
        restore_memory_backup=backup_path,
        overwrite_backup=False,
    )

    assert await async_main(args) == 0

    with sqlite3.connect(live_path) as connection:
        assert connection.execute("SELECT event_id FROM events ORDER BY event_id").fetchall() == [
            ("evt_backup",)
        ]


@pytest.mark.asyncio
async def test_cli_restore_rejects_an_active_profile(tmp_path, capsys) -> None:
    config_path = tmp_path / "companion.yaml"
    live_path = tmp_path / "memory.db"
    backup_path = tmp_path / "backup.db"
    config_path.write_text(
        f"""providers:
  memory:
    type: sqlite
    db_path: {live_path.as_posix()}
""",
        encoding="utf-8",
    )
    memory = MemoryService(MemoryServiceConfig(db_path=str(live_path)))
    try:
        await memory.backup_to(backup_path)
    finally:
        await memory.shutdown()
    owner = SingleInstanceGuard.for_memory_path(str(live_path))
    owner.acquire()
    try:
        result = await async_main(
            Namespace(
                config=config_path,
                doctor=False,
                doctor_online=False,
                doctor_json=False,
                doctor_voice_hardware=False,
                voice_input=False,
                voice=False,
                backup_memory=None,
                verify_memory_backup=None,
                restore_memory_backup=backup_path,
                overwrite_backup=False,
            )
        )
    finally:
        owner.release()

    assert result == 1
    assert "Memory restore unavailable" in capsys.readouterr().err
