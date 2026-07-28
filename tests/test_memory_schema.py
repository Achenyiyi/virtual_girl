from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from companion.memory.memory_service import (
    MEMORY_APPLICATION_ID,
    MEMORY_SCHEMA_VERSION,
    MemoryService,
    MemoryServiceConfig,
)


def read_pragma(path, pragma: str) -> int:
    with closing(sqlite3.connect(path)) as connection:
        return int(connection.execute(f"PRAGMA {pragma}").fetchone()[0])


def set_schema_markers(path, *, application_id: int, user_version: int) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f"PRAGMA application_id = {application_id}")
        connection.execute(f"PRAGMA user_version = {user_version}")


@pytest.mark.asyncio
async def test_new_database_records_ownership_and_schema_version(tmp_path) -> None:
    path = tmp_path / "memory.db"
    memory = MemoryService(MemoryServiceConfig(db_path=str(path)))
    try:
        await memory.append_event({"event_id": "evt_new", "event_type": "test.event"})
    finally:
        await memory.shutdown()

    assert read_pragma(path, "application_id") == MEMORY_APPLICATION_ID
    assert read_pragma(path, "user_version") == MEMORY_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_existing_legacy_companion_database_is_claimed_without_data_loss(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    memory = MemoryService(MemoryServiceConfig(db_path=str(path)))
    try:
        await memory.append_event({"event_id": "evt_keep", "event_type": "test.event"})
    finally:
        await memory.shutdown()
    set_schema_markers(path, application_id=0, user_version=0)

    reopened = MemoryService(MemoryServiceConfig(db_path=str(path)))
    try:
        assert await reopened.get_event("evt_keep") is not None
    finally:
        await reopened.shutdown()

    assert read_pragma(path, "application_id") == MEMORY_APPLICATION_ID
    assert read_pragma(path, "user_version") == MEMORY_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_unrelated_sqlite_file_is_not_modified(tmp_path) -> None:
    path = tmp_path / "unrelated.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE personal_notes (note TEXT)")
        connection.execute("INSERT INTO personal_notes VALUES ('keep')")
        connection.commit()

    memory = MemoryService(MemoryServiceConfig(db_path=str(path)))
    try:
        assert (await memory.health_check()).value == "unhealthy"
    finally:
        await memory.shutdown()

    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT note FROM personal_notes").fetchone() == ("keep",)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "events" not in tables
    assert not path.with_name(path.name + "-wal").exists()
    assert not path.with_name(path.name + "-shm").exists()


@pytest.mark.asyncio
async def test_legacy_backup_remains_verifiable_after_schema_markers_are_added(tmp_path) -> None:
    path = tmp_path / "legacy-backup.db"
    memory = MemoryService(MemoryServiceConfig(db_path=str(path)))
    try:
        await memory.append_event({"event_id": "evt_legacy", "event_type": "test.event"})
    finally:
        await memory.shutdown()
    set_schema_markers(path, application_id=0, user_version=0)

    assert MemoryService.verify_backup(path) == (0, True)


@pytest.mark.asyncio
async def test_future_schema_version_fails_closed_without_downgrade(tmp_path) -> None:
    path = tmp_path / "future.db"
    memory = MemoryService(MemoryServiceConfig(db_path=str(path)))
    try:
        await memory.append_event({"event_id": "evt_future", "event_type": "test.event"})
    finally:
        await memory.shutdown()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f"PRAGMA user_version = {MEMORY_SCHEMA_VERSION + 1}")

    reopened = MemoryService(MemoryServiceConfig(db_path=str(path)))
    try:
        assert (await reopened.health_check()).value == "unhealthy"
    finally:
        await reopened.shutdown()

    assert read_pragma(path, "user_version") == MEMORY_SCHEMA_VERSION + 1


@pytest.mark.asyncio
async def test_incomplete_owned_schema_fails_closed(tmp_path) -> None:
    path = tmp_path / "incomplete.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f"PRAGMA application_id = {MEMORY_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {MEMORY_SCHEMA_VERSION}")
        connection.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY)")

    memory = MemoryService(MemoryServiceConfig(db_path=str(path)))
    try:
        assert (await memory.health_check()).value == "unhealthy"
    finally:
        await memory.shutdown()


@pytest.mark.asyncio
async def test_legacy_schema_missing_runtime_column_is_not_claimed(tmp_path) -> None:
    path = tmp_path / "incomplete-legacy.db"
    memory = MemoryService(MemoryServiceConfig(db_path=str(path)))
    try:
        await memory.append_event({"event_id": "evt_original", "event_type": "test.event"})
    finally:
        await memory.shutdown()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA application_id = 0")
        connection.execute("PRAGMA user_version = 0")
        connection.execute("ALTER TABLE events RENAME TO events_complete")
        connection.execute(
            "CREATE TABLE events AS SELECT "
            "event_id, event_type, occurred_at, recorded_at, actors, privacy, severity, "
            "source_turn_ids, source_prior_event_ids, payload_json, schema_version "
            "FROM events_complete"
        )

    reopened = MemoryService(MemoryServiceConfig(db_path=str(path)))
    try:
        assert (await reopened.health_check()).value == "unhealthy"
    finally:
        await reopened.shutdown()

    assert read_pragma(path, "application_id") == 0
    assert read_pragma(path, "user_version") == 0


@pytest.mark.asyncio
async def test_partially_marked_database_is_not_claimed_as_legacy(tmp_path) -> None:
    path = tmp_path / "partially-marked.db"
    memory = MemoryService(MemoryServiceConfig(db_path=str(path)))
    try:
        await memory.append_event({"event_id": "evt_original", "event_type": "test.event"})
    finally:
        await memory.shutdown()
    set_schema_markers(path, application_id=0, user_version=MEMORY_SCHEMA_VERSION)

    reopened = MemoryService(MemoryServiceConfig(db_path=str(path)))
    try:
        assert (await reopened.health_check()).value == "unhealthy"
    finally:
        await reopened.shutdown()

    assert read_pragma(path, "application_id") == 0
    assert read_pragma(path, "user_version") == MEMORY_SCHEMA_VERSION
