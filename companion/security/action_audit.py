"""Append-only, tamper-evident SQLite audit storage for computer actions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import aiosqlite

from companion.events.base import generate_ulid
from companion.security.redaction import redact_mapping


@dataclass(frozen=True)
class ActionAuditEntry:
    action_id: str
    stage: str
    action_type: str
    risk_level: str
    success: bool | None = None
    recorded_at: float = field(default_factory=time.time)
    details: dict[str, Any] = field(default_factory=dict)
    audit_id: str = field(default_factory=lambda: f"aud_{generate_ulid()}")
    previous_hash: str = ""
    record_hash: str = ""


class ActionAuditStore(Protocol):
    async def append(self, entry: ActionAuditEntry) -> ActionAuditEntry: ...

    async def query(
        self, limit: int = 100, action_id: str | None = None
    ) -> list[ActionAuditEntry]: ...

    async def verify_chain(self) -> bool: ...

    async def shutdown(self) -> None: ...


class SQLiteActionAuditStore:
    """Durable append-only ledger with a SHA-256 chain and mutation-blocking triggers."""

    def __init__(self, db_path: str) -> None:
        if not db_path:
            raise ValueError("Action audit database path must not be empty")
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()

    async def _ensure_db(self) -> aiosqlite.Connection:
        async with self._init_lock:
            if self._conn is not None:
                return self._conn
            if self._db_path != ":memory:":
                Path(self._db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
            conn = await aiosqlite.connect(self._db_path)
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=FULL")
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS action_audit (
                       sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                       audit_id TEXT NOT NULL UNIQUE,
                       action_id TEXT NOT NULL,
                       stage TEXT NOT NULL,
                       action_type TEXT NOT NULL,
                       risk_level TEXT NOT NULL,
                       success INTEGER,
                       recorded_at REAL NOT NULL,
                       details_json TEXT NOT NULL,
                       previous_hash TEXT NOT NULL,
                       record_hash TEXT NOT NULL UNIQUE
                   )"""
            )
            await conn.execute(
                """CREATE TRIGGER IF NOT EXISTS action_audit_no_update
                   BEFORE UPDATE ON action_audit
                   BEGIN SELECT RAISE(ABORT, 'action audit is append-only'); END"""
            )
            await conn.execute(
                """CREATE TRIGGER IF NOT EXISTS action_audit_no_delete
                   BEFORE DELETE ON action_audit
                   BEGIN SELECT RAISE(ABORT, 'action audit is append-only'); END"""
            )
            await conn.commit()
            self._conn = conn
            return conn

    @staticmethod
    def _canonical_body(entry: ActionAuditEntry, previous_hash: str) -> dict[str, Any]:
        return {
            "audit_id": entry.audit_id,
            "action_id": entry.action_id,
            "stage": entry.stage,
            "action_type": entry.action_type,
            "risk_level": entry.risk_level,
            "success": entry.success,
            "recorded_at": entry.recorded_at,
            "details": redact_mapping(entry.details),
            "previous_hash": previous_hash,
        }

    @staticmethod
    def _hash_body(body: dict[str, Any]) -> str:
        canonical = json.dumps(
            body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    async def append(self, entry: ActionAuditEntry) -> ActionAuditEntry:
        async with self._lock:
            conn = await self._ensure_db()
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = await conn.execute(
                    "SELECT record_hash FROM action_audit ORDER BY sequence DESC LIMIT 1"
                )
                row = await cursor.fetchone()
                previous_hash = str(row["record_hash"]) if row else ""
                body = self._canonical_body(entry, previous_hash)
                record_hash = self._hash_body(body)
                stored = ActionAuditEntry(
                    audit_id=entry.audit_id,
                    action_id=entry.action_id,
                    stage=entry.stage,
                    action_type=entry.action_type,
                    risk_level=entry.risk_level,
                    success=entry.success,
                    recorded_at=entry.recorded_at,
                    details=body["details"],
                    previous_hash=previous_hash,
                    record_hash=record_hash,
                )
                await conn.execute(
                    """INSERT INTO action_audit
                       (audit_id, action_id, stage, action_type, risk_level, success,
                        recorded_at, details_json, previous_hash, record_hash)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        stored.audit_id,
                        stored.action_id,
                        stored.stage,
                        stored.action_type,
                        stored.risk_level,
                        None if stored.success is None else int(stored.success),
                        stored.recorded_at,
                        json.dumps(stored.details, ensure_ascii=False, sort_keys=True),
                        stored.previous_hash,
                        stored.record_hash,
                    ),
                )
                await conn.commit()
                return stored
            except Exception:
                await conn.rollback()
                raise

    @staticmethod
    def _entry_from_row(row: aiosqlite.Row) -> ActionAuditEntry:
        raw_success = row["success"]
        return ActionAuditEntry(
            audit_id=str(row["audit_id"]),
            action_id=str(row["action_id"]),
            stage=str(row["stage"]),
            action_type=str(row["action_type"]),
            risk_level=str(row["risk_level"]),
            success=None if raw_success is None else bool(raw_success),
            recorded_at=float(row["recorded_at"]),
            details=json.loads(row["details_json"]),
            previous_hash=str(row["previous_hash"]),
            record_hash=str(row["record_hash"]),
        )

    async def query(self, limit: int = 100, action_id: str | None = None) -> list[ActionAuditEntry]:
        conn = await self._ensure_db()
        safe_limit = max(1, min(limit, 10_000))
        if action_id:
            cursor = await conn.execute(
                "SELECT * FROM action_audit WHERE action_id = ? ORDER BY sequence DESC LIMIT ?",
                (action_id, safe_limit),
            )
        else:
            cursor = await conn.execute(
                "SELECT * FROM action_audit ORDER BY sequence DESC LIMIT ?", (safe_limit,)
            )
        return [self._entry_from_row(row) for row in await cursor.fetchall()]

    async def verify_chain(self) -> bool:
        conn = await self._ensure_db()
        cursor = await conn.execute("SELECT * FROM action_audit ORDER BY sequence ASC")
        previous_hash = ""
        for row in await cursor.fetchall():
            entry = self._entry_from_row(row)
            if entry.previous_hash != previous_hash:
                return False
            body = self._canonical_body(entry, previous_hash)
            if self._hash_body(body) != entry.record_hash:
                return False
            previous_hash = entry.record_hash
        return True

    async def shutdown(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
