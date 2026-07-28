"""Memory Service — SQLite-backed five-layer memory system.

Architecture:
- events table: immutable append-only event log
- facts table: semantic facts with validity ranges
- episodes table: episodic memories
- reflections table: synthesized insights
- FTS5 virtual tables for full-text search
- All derived layers rebuildable from events table alone
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sqlite3
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any

import aiosqlite

from companion.events.base import BaseEvent
from companion.providers.base import ProviderCapability, ProviderHealth, ProviderInfo
from companion.providers.memory import (
    Episode,
    EventQuery,
    MemoryProvider,
    Reflection,
    SemanticFact,
)

logger = logging.getLogger(__name__)

def _serialized_operation(method: Callable[..., Any]) -> Callable[..., Any]:
    """Keep each public memory operation atomic on the shared connection."""

    @wraps(method)
    async def wrapper(self: MemoryService, *args: Any, **kwargs: Any) -> Any:
        async with self._database_operation():
            return await method(self, *args, **kwargs)

    return wrapper

MEMORY_APPLICATION_ID = int.from_bytes(b"VCMP", "big")
MEMORY_SCHEMA_VERSION = 1
_REQUIRED_SCHEMA: dict[str, set[str]] = {
    "events": {
        "event_id",
        "event_type",
        "occurred_at",
        "recorded_at",
        "actors",
        "privacy",
        "severity",
        "source_turn_ids",
        "source_prior_event_ids",
        "payload_json",
        "content_hash",
        "schema_version",
    },
    "facts": {
        "fact_id",
        "key",
        "value",
        "category",
        "confidence",
        "valid_from",
        "valid_to",
        "source_event_ids",
        "extraction_method",
        "created_at",
    },
    "episodes": {
        "episode_id",
        "title",
        "summary",
        "participants",
        "emotional_salience",
        "turn_ids",
        "tags",
        "occurred_at",
        "created_at",
    },
    "reflections": {
        "reflection_id",
        "content",
        "category",
        "source_event_ids",
        "source_episode_ids",
        "confidence",
        "generated_plan",
        "created_at",
    },
}


@dataclass
class MemoryServiceConfig:
    """Configuration for the SQLite-backed memory service."""

    db_path: str = "companion_memory.db"
    wal_mode: bool = True
    fts_enabled: bool = True
    max_event_log_size: int = 1_000_000
    auto_vacuum: bool = True


_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS events (
        event_id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        actors TEXT NOT NULL DEFAULT '[]',
        privacy TEXT NOT NULL DEFAULT 'private',
        severity TEXT NOT NULL DEFAULT 'info',
        source_turn_ids TEXT NOT NULL DEFAULT '[]',
        source_prior_event_ids TEXT NOT NULL DEFAULT '[]',
        payload_json TEXT NOT NULL DEFAULT '{}',
        content_hash TEXT,
        schema_version INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
    CREATE INDEX IF NOT EXISTS idx_events_occurred ON events(occurred_at);
    CREATE INDEX IF NOT EXISTS idx_events_privacy ON events(privacy);

    CREATE TABLE IF NOT EXISTS facts (
        fact_id TEXT PRIMARY KEY,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'general',
        confidence REAL NOT NULL DEFAULT 1.0,
        valid_from TEXT NOT NULL,
        valid_to TEXT,
        source_event_ids TEXT NOT NULL DEFAULT '[]',
        extraction_method TEXT NOT NULL DEFAULT 'llm',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_facts_key ON facts(key);
    CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category);
    CREATE INDEX IF NOT EXISTS idx_facts_valid ON facts(valid_from, valid_to);

    CREATE TABLE IF NOT EXISTS episodes (
        episode_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        participants TEXT NOT NULL DEFAULT '["user","companion"]',
        emotional_salience REAL NOT NULL DEFAULT 0.5,
        turn_ids TEXT NOT NULL DEFAULT '[]',
        tags TEXT NOT NULL DEFAULT '[]',
        occurred_at TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_episodes_occurred ON episodes(occurred_at);
    CREATE INDEX IF NOT EXISTS idx_episodes_salience ON episodes(emotional_salience);

    CREATE TABLE IF NOT EXISTS reflections (
        reflection_id TEXT PRIMARY KEY,
        content TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'general',
        source_event_ids TEXT NOT NULL DEFAULT '[]',
        source_episode_ids TEXT NOT NULL DEFAULT '[]',
        confidence REAL NOT NULL DEFAULT 0.5,
        generated_plan TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_reflections_category ON reflections(category);
    CREATE INDEX IF NOT EXISTS idx_reflections_created ON reflections(created_at);

    CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
        key, value, content=facts, content_rowid=rowid
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
        title, summary, content=episodes, content_rowid=rowid
    );

    CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
        INSERT INTO facts_fts(rowid, key, value) VALUES (new.rowid, new.key, new.value);
    END;
    CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
        INSERT INTO facts_fts(facts_fts, rowid, key, value)
        VALUES ('delete', old.rowid, old.key, old.value);
    END;
    CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
        INSERT INTO facts_fts(facts_fts, rowid, key, value)
        VALUES ('delete', old.rowid, old.key, old.value);
        INSERT INTO facts_fts(rowid, key, value) VALUES (new.rowid, new.key, new.value);
    END;
    CREATE TRIGGER IF NOT EXISTS episodes_ai AFTER INSERT ON episodes BEGIN
        INSERT INTO episodes_fts(rowid, title, summary) VALUES (new.rowid, new.title, new.summary);
    END;
    CREATE TRIGGER IF NOT EXISTS episodes_ad AFTER DELETE ON episodes BEGIN
        INSERT INTO episodes_fts(episodes_fts, rowid, title, summary)
        VALUES ('delete', old.rowid, old.title, old.summary);
    END;
    CREATE TRIGGER IF NOT EXISTS episodes_au AFTER UPDATE ON episodes BEGIN
        INSERT INTO episodes_fts(episodes_fts, rowid, title, summary)
        VALUES ('delete', old.rowid, old.title, old.summary);
        INSERT INTO episodes_fts(rowid, title, summary) VALUES (new.rowid, new.title, new.summary);
    END;
"""


class MemoryService(MemoryProvider):
    """SQLite-backed implementation of the five-layer memory system.

    Uses aiosqlite for non-blocking async database access. All derived layers
    can be rebuilt from the event log alone, satisfying the "可恢复性"
    (recoverability) requirement.
    """

    def __init__(self, config: MemoryServiceConfig | None = None) -> None:
        self._config = config or MemoryServiceConfig()
        self._conn: aiosqlite.Connection | None = None
        self._initialized = False
        self._init_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._operation_owner: asyncio.Task[Any] | None = None
        self._operation_depth = 0
        self._transaction_owner: asyncio.Task[Any] | None = None
        self._backup_lock = asyncio.Lock()

    async def _commit(self, conn: aiosqlite.Connection) -> None:
        if self._transaction_owner is not asyncio.current_task():
            await conn.commit()

    @asynccontextmanager
    async def _database_operation(self) -> AsyncIterator[None]:
        """Serialize access to the shared connection, with same-task re-entry."""
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("memory operation requires an asyncio task")
        if self._operation_owner is task:
            self._operation_depth += 1
            try:
                yield
            finally:
                self._operation_depth -= 1
            return

        await self._operation_lock.acquire()
        self._operation_owner = task
        self._operation_depth = 1
        try:
            yield
        finally:
            self._operation_depth = 0
            self._operation_owner = None
            self._operation_lock.release()

    async def _ensure_db(self) -> aiosqlite.Connection:
        if self._conn is not None:
            return self._conn
        async with self._init_lock:
            existing: aiosqlite.Connection | None = self.__dict__["_conn"]
            if existing is not None:
                return existing
            if self._config.db_path != ":memory:":
                Path(self._config.db_path).expanduser().resolve().parent.mkdir(
                    parents=True, exist_ok=True
                )
            self._conn = await aiosqlite.connect(self._config.db_path)
            self._conn.row_factory = aiosqlite.Row
            try:
                await self._conn.execute("PRAGMA foreign_keys=ON")
                await self._init_schema()
                if self._config.wal_mode:
                    async with self._conn.execute("PRAGMA journal_mode=WAL") as cursor:
                        await cursor.fetchone()
            except BaseException:
                await self._conn.close()
                self._conn = None
                self._initialized = False
                raise
            return self._conn

    async def _connection(self) -> aiosqlite.Connection:
        """Return the initialized connection without re-entering schema setup."""
        if self._conn is None:
            await self._ensure_db()
        if self._conn is None:
            raise RuntimeError("memory database connection is unavailable")
        return self._conn

    async def _init_schema(self) -> None:
        if self._initialized:
            return
        conn = await self._connection()
        application_row = await (await conn.execute("PRAGMA application_id")).fetchone()
        version_row = await (await conn.execute("PRAGMA user_version")).fetchone()
        if application_row is None or version_row is None:
            raise RuntimeError("SQLite schema metadata is unavailable")
        application_id = int(application_row[0])
        schema_version = int(version_row[0])
        table_names = await self._table_names(conn)
        user_tables = {name for name in table_names if not name.startswith("sqlite_")}

        if application_id not in {0, MEMORY_APPLICATION_ID}:
            raise RuntimeError("SQLite file belongs to a different application")
        if schema_version > MEMORY_SCHEMA_VERSION:
            raise RuntimeError(
                f"memory schema v{schema_version} is newer than supported v{MEMORY_SCHEMA_VERSION}"
            )

        is_empty = not user_tables
        is_legacy = (
            application_id == 0
            and schema_version == 0
            and self._required_tables_present(table_names)
        )
        if application_id == 0 and not is_empty and not is_legacy:
            raise RuntimeError("unrecognized SQLite file; refusing to add companion tables")
        if not is_empty:
            await self._validate_required_columns_async(conn)

        await conn.executescript(_SCHEMA_SQL)
        await conn.execute(f"PRAGMA application_id = {MEMORY_APPLICATION_ID}")
        await conn.execute(f"PRAGMA user_version = {MEMORY_SCHEMA_VERSION}")
        await conn.commit()
        self._initialized = True
        logger.info(
            "Memory service schema v%d initialized at %s",
            MEMORY_SCHEMA_VERSION,
            self._config.db_path,
        )

    @staticmethod
    async def _table_names(conn: aiosqlite.Connection) -> set[str]:
        cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {str(row[0]) for row in await cursor.fetchall()}

    @staticmethod
    def _required_tables_present(table_names: set[str]) -> bool:
        return _REQUIRED_SCHEMA.keys() <= table_names

    @staticmethod
    async def _validate_required_columns_async(conn: aiosqlite.Connection) -> None:
        table_names = await MemoryService._table_names(conn)
        missing_tables = _REQUIRED_SCHEMA.keys() - table_names
        if missing_tables:
            raise RuntimeError(f"memory database is missing tables: {sorted(missing_tables)}")
        for table, required_columns in _REQUIRED_SCHEMA.items():
            cursor = await conn.execute(f"PRAGMA table_info({table})")
            columns = {str(row[1]) for row in await cursor.fetchall()}
            missing_columns = required_columns - columns
            if missing_columns:
                raise RuntimeError(
                    f"memory table {table} is missing columns: {sorted(missing_columns)}"
                )

    # ── Event Log (Layer 1) ───────────────────────────────────────────

    @_serialized_operation
    async def append_domain_event(self, event: BaseEvent) -> str:
        """Persist a typed domain event without losing header metadata."""
        event_id: str = await self.append_event(
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "header": event.header.model_dump(mode="json"),
                "payload": event.model_dump(mode="json", exclude={"header"}),
            }
        )
        return event_id

    @_serialized_operation
    async def append_event(self, event_data: dict[str, Any]) -> str:
        conn = await self._ensure_db()
        event_id = event_data.get("event_id", "")
        if not event_id:
            from companion.events.base import generate_ulid

            event_id = f"evt_{generate_ulid()}"
            event_data["event_id"] = event_id
        header = event_data.get("header", {})
        await conn.execute(
            """INSERT INTO events
               (event_id, event_type, occurred_at, recorded_at, actors,
                privacy, severity, source_turn_ids, source_prior_event_ids,
                payload_json, content_hash, schema_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                event_data.get("event_type", header.get("event_type", "unknown")),
                str(event_data.get("occurred_at", header.get("occurred_at", datetime.now(UTC)))),
                str(datetime.now(UTC)),
                json.dumps(event_data.get("actors", header.get("actors", []))),
                str(event_data.get("privacy", header.get("privacy", "private"))),
                str(event_data.get("severity", header.get("severity", "info"))),
                json.dumps(
                    event_data.get("source", {}).get(
                        "turn_ids", (header.get("source", {}) or {}).get("turn_ids", [])
                    )
                ),
                json.dumps(
                    event_data.get("source", {}).get(
                        "prior_event_ids",
                        (header.get("source", {}) or {}).get("prior_event_ids", []),
                    )
                ),
                json.dumps(event_data.get("payload", {})),
                header.get("content_hash"),
                header.get("schema_version", 1),
            ),
        )
        await self._commit(conn)
        return str(event_id)

    @_serialized_operation
    async def query_events(self, query: EventQuery) -> list[dict[str, Any]]:
        conn = await self._ensure_db()
        conditions = ["1=1"]
        params: list[Any] = []
        if query.event_types:
            placeholders = ",".join("?" for _ in query.event_types)
            conditions.append(f"event_type IN ({placeholders})")
            params.extend(query.event_types)
        if query.start_time:
            conditions.append("occurred_at >= ?")
            params.append(str(query.start_time))
        if query.end_time:
            conditions.append("occurred_at <= ?")
            params.append(str(query.end_time))
        if query.privacy_levels:
            placeholders = ",".join("?" for _ in query.privacy_levels)
            conditions.append(f"privacy IN ({placeholders})")
            params.extend(query.privacy_levels)
        order = "ASC" if query.sort_ascending else "DESC"
        sql = f"""SELECT * FROM events WHERE {" AND ".join(conditions)}
                   ORDER BY occurred_at {order} LIMIT ? OFFSET ?"""
        params.extend([query.limit, query.offset])
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    @_serialized_operation
    async def get_event(self, event_id: str) -> dict[str, Any] | None:
        conn = await self._ensure_db()
        cursor = await conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    # ── Semantic Facts (Layer 3) ──────────────────────────────────────

    @_serialized_operation
    async def upsert_fact(self, fact: SemanticFact) -> str:
        conn = await self._ensure_db()
        now = datetime.now(UTC).isoformat()
        await conn.execute(
            "UPDATE facts SET valid_to = ? WHERE key = ? AND valid_to IS NULL", (now, fact.key)
        )
        await conn.execute(
            """INSERT INTO facts
               (fact_id, key, value, category, confidence, valid_from, valid_to,
                source_event_ids, extraction_method)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fact.fact_id,
                fact.key,
                fact.value,
                fact.category,
                fact.confidence,
                (fact.valid_from or datetime.now(UTC)).isoformat(),
                (fact.valid_to.isoformat() if fact.valid_to else None),
                json.dumps(fact.source_event_ids),
                fact.extraction_method,
            ),
        )
        await self._commit(conn)
        return fact.fact_id

    @_serialized_operation
    async def get_fact(self, key: str) -> SemanticFact | None:
        conn = await self._ensure_db()
        cursor = await conn.execute(
            "SELECT * FROM facts WHERE key = ? AND valid_to IS NULL "
            "ORDER BY valid_from DESC LIMIT 1",
            (key,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return SemanticFact(
            fact_id=row["fact_id"],
            key=row["key"],
            value=row["value"],
            category=row["category"],
            confidence=row["confidence"],
            valid_from=datetime.fromisoformat(row["valid_from"]),
            valid_to=datetime.fromisoformat(row["valid_to"]) if row["valid_to"] else None,
            source_event_ids=json.loads(row["source_event_ids"]),
            extraction_method=row["extraction_method"],
        )

    @_serialized_operation
    async def search_facts(
        self, query: str, category: str | None = None, limit: int = 10
    ) -> list[SemanticFact]:
        conn = await self._ensure_db()
        # Use LIKE as primary search (works for Chinese without tokenizers).
        # FTS5 is tried first for non-CJK queries; on error falls back to LIKE.
        rows: list[aiosqlite.Row] = []
        if self._config.fts_enabled and self._has_latin_chars(query):
            try:
                fts_query = " OR ".join(f'"{term}"' for term in query.split() if term) or query
                conditions = [
                    "facts_fts MATCH ?",
                    "f.rowid = facts_fts.rowid",
                    "f.valid_to IS NULL",
                ]
                params: list[Any] = [fts_query]
                if category:
                    conditions.append("f.category = ?")
                    params.append(category)
                sql = f"SELECT f.* FROM facts f, facts_fts WHERE {' AND '.join(conditions)} LIMIT ?"
                params.append(limit)
                cursor = await conn.execute(sql, params)
                rows = list(await cursor.fetchall())
            except Exception:
                rows = []
        if not rows:
            rows = await self._search_facts_like(conn, query, category, limit)
        return [
            SemanticFact(
                fact_id=r["fact_id"],
                key=r["key"],
                value=r["value"],
                category=r["category"],
                confidence=r["confidence"],
                valid_from=datetime.fromisoformat(r["valid_from"]),
                valid_to=datetime.fromisoformat(r["valid_to"]) if r["valid_to"] else None,
                source_event_ids=json.loads(r["source_event_ids"]),
                extraction_method=r["extraction_method"],
            )
            for r in rows
        ]

    @staticmethod
    def _has_latin_chars(text: str) -> bool:
        """Check if text contains mainly Latin chars (FTS5 works with whitespace tokenization)."""
        latin_count = sum(1 for c in text if c.isascii() and c.isalpha())
        return latin_count > len(text.replace(" ", "")) * 0.3

    async def _search_facts_like(
        self, conn: aiosqlite.Connection, query: str, category: str | None, limit: int
    ) -> list[aiosqlite.Row]:
        conditions = ["(key LIKE ? OR value LIKE ?)", "valid_to IS NULL"]
        like_q = f"%{query}%"
        params: list[Any] = [like_q, like_q]
        if category:
            conditions.append("category = ?")
            params.append(category)
        sql = f"SELECT * FROM facts WHERE {' AND '.join(conditions)} LIMIT ?"
        params.append(limit)
        cursor = await conn.execute(sql, params)
        return list(await cursor.fetchall())

    @_serialized_operation
    async def list_fact_updates(self, key: str) -> list[dict[str, Any]]:
        conn = await self._ensure_db()
        cursor = await conn.execute(
            "SELECT * FROM facts WHERE key = ? ORDER BY valid_from DESC", (key,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── Episodic Memory (Layer 4) ─────────────────────────────────────

    @_serialized_operation
    async def create_episode(self, episode: Episode) -> str:
        conn = await self._ensure_db()
        await conn.execute(
            """INSERT INTO episodes
               (episode_id, title, summary, participants, emotional_salience,
                turn_ids, tags, occurred_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                episode.episode_id,
                episode.title,
                episode.summary,
                json.dumps(episode.participants),
                episode.emotional_salience,
                json.dumps(episode.turn_ids),
                json.dumps(episode.tags),
                (episode.occurred_at or datetime.now(UTC)).isoformat(),
            ),
        )
        await self._commit(conn)
        return episode.episode_id

    @_serialized_operation
    async def search_episodes(
        self, query: str, limit: int = 10, min_salience: float = 0.0
    ) -> list[Episode]:
        conn = await self._ensure_db()
        # LIKE search (works for all languages including Chinese).
        # FTS5 is attempted for Latin-heavy queries; falls back to LIKE.
        rows: list[aiosqlite.Row] = []
        if self._config.fts_enabled and self._has_latin_chars(query):
            try:
                fts_query = " OR ".join(f'"{term}"' for term in query.split() if term) or query
                conditions = [
                    "episodes_fts MATCH ?",
                    "e.rowid = episodes_fts.rowid",
                    "e.emotional_salience >= ?",
                ]
                fts_params: list[Any] = [fts_query, min_salience]
                sql = (
                    "SELECT e.* FROM episodes e, episodes_fts WHERE "
                    f"{' AND '.join(conditions)} "
                    "ORDER BY e.occurred_at DESC LIMIT ?"
                )
                fts_params.append(limit)
                cursor = await conn.execute(sql, fts_params)
                rows = list(await cursor.fetchall())
            except Exception:
                rows = []
        if not rows:
            conditions = ["emotional_salience >= ?", "(title LIKE ? OR summary LIKE ?)"]
            like_params: list[Any] = [min_salience, f"%{query}%", f"%{query}%"]
            sql = (
                f"SELECT * FROM episodes WHERE {' AND '.join(conditions)} "
                "ORDER BY occurred_at DESC LIMIT ?"
            )
            like_params.append(limit)
            cursor = await conn.execute(sql, like_params)
            rows = list(await cursor.fetchall())
        return [
            Episode(
                episode_id=r["episode_id"],
                title=r["title"],
                summary=r["summary"],
                participants=json.loads(r["participants"]),
                emotional_salience=r["emotional_salience"],
                turn_ids=json.loads(r["turn_ids"]),
                tags=json.loads(r["tags"]),
                occurred_at=datetime.fromisoformat(r["occurred_at"]),
            )
            for r in rows
        ]

    @_serialized_operation
    async def get_episode(self, episode_id: str) -> Episode | None:
        conn = await self._ensure_db()
        cursor = await conn.execute("SELECT * FROM episodes WHERE episode_id = ?", (episode_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return Episode(
            episode_id=row["episode_id"],
            title=row["title"],
            summary=row["summary"],
            participants=json.loads(row["participants"]),
            emotional_salience=row["emotional_salience"],
            turn_ids=json.loads(row["turn_ids"]),
            tags=json.loads(row["tags"]),
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
        )

    # ── Reflections (Layer 5) ─────────────────────────────────────────

    @_serialized_operation
    async def create_reflection(self, reflection: Reflection) -> str:
        conn = await self._ensure_db()
        await conn.execute(
            """INSERT INTO reflections
               (reflection_id, content, category, source_event_ids,
                source_episode_ids, confidence, generated_plan)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                reflection.reflection_id,
                reflection.content,
                reflection.category,
                json.dumps(reflection.source_event_ids),
                json.dumps(reflection.source_episode_ids),
                reflection.confidence,
                reflection.generated_plan,
            ),
        )
        await self._commit(conn)
        return reflection.reflection_id

    @_serialized_operation
    async def get_recent_reflections(self, limit: int = 10) -> list[Reflection]:
        conn = await self._ensure_db()
        cursor = await conn.execute(
            "SELECT * FROM reflections ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [
            Reflection(
                reflection_id=r["reflection_id"],
                content=r["content"],
                category=r["category"],
                source_event_ids=json.loads(r["source_event_ids"]),
                source_episode_ids=json.loads(r["source_episode_ids"]),
                confidence=r["confidence"],
                generated_plan=r["generated_plan"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    # ── Memory Management ─────────────────────────────────────────────

    @_serialized_operation
    async def forget(self, event_ids: list[str], reason: str = "user_request") -> int:
        conn = await self._ensure_db()
        cascade_count = 0
        for eid in event_ids:
            for table, col in [
                ("facts", "source_event_ids"),
                ("episodes", "turn_ids"),
                ("reflections", "source_event_ids"),
            ]:
                id_col = (
                    "fact_id"
                    if table == "facts"
                    else ("episode_id" if table == "episodes" else "reflection_id")
                )
                cursor = await conn.execute(
                    f"SELECT {id_col} FROM {table} WHERE {col} LIKE ?", (f'%"{eid}"%',)
                )
                rows = await cursor.fetchall()
                for row in rows:
                    await conn.execute(f"DELETE FROM {table} WHERE {id_col} = ?", (row[id_col],))
                    cascade_count += 1
        for eid in event_ids:
            await conn.execute("DELETE FROM events WHERE event_id = ?", (eid,))
            cascade_count += 1
        await self._commit(conn)
        logger.info(
            "Forgot %d events with %d cascade deletions (reason: %s)",
            len(event_ids),
            cascade_count,
            reason,
        )
        return cascade_count

    @_serialized_operation
    async def rebuild_from_log(self) -> dict[str, Any]:
        """Rebuild derived memory from the event log.

        Deletes all derived data (facts, episodes, reflections) and
        replays events to rebuild them. This validates that the event
        log is a complete source of truth.
        """
        t0 = time.time()
        conn = await self._ensure_db()

        # Count source events before clearing
        cursor = await conn.execute("SELECT COUNT(*) FROM events")
        row = await cursor.fetchone()
        event_count = row[0] if row else 0

        await conn.execute("BEGIN IMMEDIATE")
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("memory rebuild requires an asyncio task")
        self._transaction_owner = task
        try:
            result = await self._rebuild_derived_layers(conn, event_count, t0)
            await conn.commit()
            logger.info("Memory rebuild completed: %s", result)
            return result
        except asyncio.CancelledError:
            await conn.rollback()
            logger.warning("Memory rebuild cancelled; previous memory remains intact")
            raise
        except Exception as exc:
            await conn.rollback()
            logger.exception("Memory rebuild rolled back; previous memory remains intact")
            raise RuntimeError(
                "Memory rebuild failed; previous derived memory was restored"
            ) from exc
        finally:
            self._transaction_owner = None

    async def _rebuild_derived_layers(
        self,
        conn: aiosqlite.Connection,
        event_count: int,
        t0: float,
    ) -> dict[str, Any]:
        """Rebuild derived tables inside the caller's transaction."""
        await conn.execute("DELETE FROM reflections")
        await conn.execute("DELETE FROM episodes")
        await conn.execute("DELETE FROM facts")

        # Replay events to rebuild derived layers
        facts_restored = 0
        episodes_restored = 0
        reflections_restored = 0
        consistency_errors = 0

        # Replay conversation turn events → facts via FactExtractor
        try:
            from companion.memory.fact_extractor import FactExtractor

            extractor = FactExtractor()
            cursor = await conn.execute(
                "SELECT event_id, payload_json FROM events "
                "WHERE event_type LIKE 'conversation.turn.%'"
            )
            event_rows = await cursor.fetchall()
            for evt_row in event_rows:
                try:
                    payload = json.loads(evt_row["payload_json"])
                    user_text = payload.get("user_text", "")
                    if user_text:
                        extraction = extractor.extract(user_text, [evt_row["event_id"]])
                        for fact in extraction.facts:
                            await self.upsert_fact(fact)
                            facts_restored += 1
                except (json.JSONDecodeError, KeyError) as exc:
                    raise ValueError(
                        f"Invalid conversation event payload: {evt_row['event_id']}"
                    ) from exc
        except Exception as e:
            logger.exception("Error replaying facts during rebuild: %s", e)
            consistency_errors += 1

        # Replay episode-related events → episodes via EpisodeSegmenter
        try:
            from companion.memory.episode_segmenter import EpisodeSegmenter

            segmenter = EpisodeSegmenter()
            cursor = await conn.execute(
                "SELECT * FROM events "
                "WHERE event_type = 'conversation.turn.completed' "
                "ORDER BY occurred_at ASC"
            )
            completed_rows = await cursor.fetchall()
            turns: list[dict[str, Any]] = []
            for cr in completed_rows:
                try:
                    payload = json.loads(cr["payload_json"]) if cr["payload_json"] else {}
                    turns.append(
                        {
                            "turn_id": cr["event_id"],
                            "user_text": payload.get("user_text", ""),
                            "companion_text": payload.get("companion_text", ""),
                            "timestamp": datetime.fromisoformat(cr["occurred_at"])
                            if cr["occurred_at"]
                            else datetime.now(UTC),
                        }
                    )
                except (json.JSONDecodeError, KeyError) as exc:
                    raise ValueError(f"Invalid completed-turn payload: {cr['event_id']}") from exc
            if turns:
                seg_result = segmenter.segment(turns)
                for ep in seg_result.episodes:
                    await self.create_episode(ep)
                    episodes_restored += 1
        except Exception as e:
            logger.exception("Error replaying episodes during rebuild: %s", e)
            consistency_errors += 1

        # Replay reflection-relevant events
        try:
            cursor = await conn.execute(
                "SELECT event_id, event_type FROM events ORDER BY occurred_at ASC"
            )
            all_rows = await cursor.fetchall()
            if all_rows:
                from companion.memory.reflection_engine import ReflectionConfig, ReflectionEngine

                engine = ReflectionEngine(
                    ReflectionConfig(
                        importance_threshold=0.3,
                        min_events_for_reflection=1,
                        max_reflections_per_day=10000,
                        max_reflections_per_hour=10000,
                        min_interval_seconds=0,
                    )
                )
                for ar in all_rows:
                    ref = engine.feed_event(ar["event_id"], importance=0.3)
                    if ref:
                        await self.create_reflection(ref)
                        reflections_restored += 1
        except Exception as e:
            logger.exception("Error replaying reflections during rebuild: %s", e)
            consistency_errors += 1

        # Verify table integrity
        for table in ["events", "facts", "episodes", "reflections"]:
            try:
                await conn.execute(f"SELECT COUNT(*) FROM {table}")
            except Exception:
                consistency_errors += 1

        if consistency_errors:
            raise RuntimeError(
                f"Memory rebuild encountered {consistency_errors} consistency error(s)"
            )

        duration_ms = int((time.time() - t0) * 1000)
        rebuild_result = {
            "event_count": event_count,
            "facts_restored": facts_restored,
            "episodes_restored": episodes_restored,
            "reflections_restored": reflections_restored,
            "consistency_errors": consistency_errors,
            "passed_consistency_check": consistency_errors == 0,
            "duration_ms": duration_ms,
        }
        return rebuild_result

    @_serialized_operation
    async def verify_consistency(self) -> dict[str, Any]:
        conn = await self._ensure_db()
        errors: list[str] = []
        for table, col in [("facts", "source_event_ids"), ("episodes", "turn_ids")]:
            id_col = "fact_id" if table == "facts" else "episode_id"
            cursor = await conn.execute(f"SELECT {id_col}, {col} FROM {table}")
            rows = await cursor.fetchall()
            for row in rows:
                for eid in json.loads(row[col]):
                    c2 = await conn.execute("SELECT 1 FROM events WHERE event_id = ?", (eid,))
                    r2 = await c2.fetchone()
                    if not r2:
                        errors.append(
                            f"{table[:-1].capitalize()} {row[id_col]} "
                            f"references missing event {eid}"
                        )
        return {
            "is_consistent": len(errors) == 0,
            "error_count": len(errors),
            "error_details": errors,
        }

    async def backup_to(self, destination: str | Path, *, overwrite: bool = False) -> Path:
        """Create and verify a consistent SQLite backup without stopping the runtime."""
        if self._config.db_path == ":memory:":
            raise ValueError("in-memory databases cannot be backed up to a release file")

        target = Path(destination).expanduser().resolve()
        source = Path(self._config.db_path).expanduser().resolve()
        if target == source:
            raise ValueError("backup destination must differ from the live database")
        if target.exists() and not overwrite:
            raise FileExistsError(f"backup destination already exists: {target}")

        async with self._backup_lock:
            if target.exists() and not overwrite:
                raise FileExistsError(f"backup destination already exists: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(
                f".{target.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
            )

            try:
                async with self._database_operation():
                    conn = await self._ensure_db()
                    if conn.in_transaction:
                        raise RuntimeError("cannot back up memory during an active transaction")
                    backup_task = asyncio.create_task(
                        asyncio.to_thread(self._backup_sqlite_file, source, temporary)
                    )
                    try:
                        await asyncio.shield(backup_task)
                    except asyncio.CancelledError:
                        await backup_task
                        raise
                self.verify_backup(temporary)
                if overwrite:
                    os.replace(temporary, target)
                else:
                    os.link(temporary, target)
                    temporary.unlink()
                return target
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise

    @staticmethod
    def _backup_sqlite_file(source: Path, destination: Path) -> None:
        """Run SQLite's online backup on isolated synchronous connections."""
        source_uri = source.as_uri() + "?mode=ro"
        with (
            closing(sqlite3.connect(source_uri, uri=True, timeout=5.0)) as source_conn,
            closing(sqlite3.connect(destination, timeout=5.0)) as destination_conn,
        ):
            source_conn.backup(destination_conn)

    @staticmethod
    def verify_backup(path: str | Path) -> tuple[int, bool]:
        """Reject a missing, corrupt, or structurally incomplete memory backup."""
        backup_path = Path(path).expanduser().resolve()
        if not backup_path.is_file():
            raise FileNotFoundError(f"memory backup does not exist: {backup_path}")
        uri = backup_path.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as connection:
            quick_check = connection.execute("PRAGMA quick_check").fetchone()
            if not quick_check or quick_check[0] != "ok":
                raise sqlite3.DatabaseError("memory backup integrity check failed")
            return MemoryService.validate_connection_schema(connection, allow_legacy=True)

    @staticmethod
    def validate_connection_schema(
        connection: sqlite3.Connection, *, allow_legacy: bool
    ) -> tuple[int, bool]:
        """Validate ownership/version/shape and return (version, is_legacy)."""
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        table_names = {str(row[0]) for row in rows}
        if application_id not in {0, MEMORY_APPLICATION_ID}:
            raise sqlite3.DatabaseError("SQLite file belongs to a different application")
        if schema_version > MEMORY_SCHEMA_VERSION:
            raise sqlite3.DatabaseError(
                f"memory schema v{schema_version} is newer than supported v{MEMORY_SCHEMA_VERSION}"
            )
        is_legacy = (
            application_id == 0
            and schema_version == 0
            and MemoryService._required_tables_present(table_names)
        )
        if application_id == 0 and not (allow_legacy and is_legacy):
            raise sqlite3.DatabaseError("memory database ownership marker is missing")
        missing_tables = _REQUIRED_SCHEMA.keys() - table_names
        if missing_tables:
            raise sqlite3.DatabaseError(
                f"memory database is missing tables: {sorted(missing_tables)}"
            )
        for table, required_columns in _REQUIRED_SCHEMA.items():
            columns = {
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
            }
            missing_columns = required_columns - columns
            if missing_columns:
                raise sqlite3.DatabaseError(
                    f"memory table {table} is missing columns: {sorted(missing_columns)}"
                )
        return schema_version, is_legacy

    # ── Provider Lifecycle ────────────────────────────────────────────

    def provider_info(self) -> ProviderInfo:
        return ProviderInfo(
            name="sqlite-memory-service",
            version="0.1.0",
            capabilities=[ProviderCapability.OFFLINE, ProviderCapability.BATCH],
        )

    @_serialized_operation
    async def health_check(self) -> ProviderHealth:
        try:
            conn = await self._ensure_db()
            await conn.execute("SELECT 1")
            return ProviderHealth.HEALTHY
        except Exception:
            logger.exception("Memory health check failed")
            return ProviderHealth.UNHEALTHY

    @_serialized_operation
    async def shutdown(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
            self._initialized = False
            logger.info("Memory service shut down")
