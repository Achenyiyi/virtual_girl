from __future__ import annotations

import sqlite3
from argparse import Namespace

import pytest

from companion.__main__ import async_main
from companion.memory.memory_service import MemoryService, MemoryServiceConfig


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
    with pytest.raises(sqlite3.DatabaseError, match="missing required tables"):
        MemoryService.verify_backup(unrelated)


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
