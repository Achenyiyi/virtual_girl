"""Production safety tests for durable action audit and sandbox enforcement."""

from __future__ import annotations

import asyncio
import json

import pytest

from companion.providers.action import SandboxStatus
from companion.security.action_audit import ActionAuditEntry, SQLiteActionAuditStore
from companion.services.action_service import ActionService, ActionServiceConfig
from tests.test_providers import MockActionProvider


@pytest.mark.asyncio
async def test_action_fails_closed_without_required_durable_audit() -> None:
    class CountingProvider(MockActionProvider):
        def __init__(self) -> None:
            self.executions = 0

        async def execute(self, request):
            self.executions += 1
            return await super().execute(request)

    provider = CountingProvider()
    service = ActionService(provider=provider)

    record, result = await service.request("read_window_title")

    assert result is not None and not result.success
    assert record.state.value == "failed"
    assert provider.executions == 0
    assert result.error_message == "Durable action audit is unavailable"


@pytest.mark.asyncio
async def test_durable_audit_is_redacted_persistent_and_chain_valid(tmp_path) -> None:
    db_path = tmp_path / "action_audit.db"
    store = SQLiteActionAuditStore(str(db_path))
    service = ActionService(provider=MockActionProvider(), audit_store=store)

    record, result = await service.request(
        "read_window_title",
        {
            "api_key": "abcdefghijklmnopqrstuvwxyz",
            "nested": {"authorization": "Bearer secret-secret-secret"},
        },
    )

    assert result is not None and result.success
    assert record.request.sandbox_id
    assert await store.verify_chain()
    await store.shutdown()

    reopened = SQLiteActionAuditStore(str(db_path))
    try:
        entries = await reopened.query(action_id=record.request.action_id)
        serialized = json.dumps(
            [entry.details for entry in entries], ensure_ascii=False, sort_keys=True
        )
        assert {entry.stage for entry in entries} == {"requested", "executing", "executed"}
        assert "abcdefghijklmnopqrstuvwxyz" not in serialized
        assert "secret-secret-secret" not in serialized
        assert "[REDACTED]" in serialized
        assert await reopened.verify_chain()
    finally:
        await reopened.shutdown()


@pytest.mark.asyncio
async def test_unverified_sandbox_prevents_provider_execution(tmp_path) -> None:
    class UnverifiedProvider(MockActionProvider):
        def __init__(self) -> None:
            self.executions = 0

        async def verify_sandbox(self, request) -> SandboxStatus:
            return SandboxStatus(verified=False, reason="isolation unavailable")

        async def execute(self, request):
            self.executions += 1
            return await super().execute(request)

    provider = UnverifiedProvider()
    store = SQLiteActionAuditStore(str(tmp_path / "action_audit.db"))
    service = ActionService(
        provider=provider,
        config=ActionServiceConfig(sandbox_enabled=True, require_durable_audit=True),
        audit_store=store,
    )
    try:
        record, result = await service.request("read_window_title")
        assert result is not None and not result.success
        assert result.error_message == "Sandbox verification failed"
        assert record.state.value == "failed"
        assert provider.executions == 0
    finally:
        await store.shutdown()


@pytest.mark.asyncio
async def test_undo_cannot_bypass_sandbox(tmp_path) -> None:
    class UndoBlockedProvider(MockActionProvider):
        def __init__(self) -> None:
            self.undo_calls = 0

        async def verify_sandbox(self, request) -> SandboxStatus:
            if request.action_type.startswith("undo:"):
                return SandboxStatus(verified=False, reason="undo isolation unavailable")
            return await super().verify_sandbox(request)

        async def undo(self, action_id):
            self.undo_calls += 1
            return await super().undo(action_id)

    provider = UndoBlockedProvider()
    store = SQLiteActionAuditStore(str(tmp_path / "action_audit.db"))
    service = ActionService(provider=provider, audit_store=store)
    try:
        record, pending_result = await service.request("create_file", {"path": "safe.txt"})
        assert pending_result is None
        executed = await service.confirm(record.request.action_id, approved=True)
        assert executed is not None and executed.success

        undone = await service.undo(record.request.action_id)
        assert undone is not None and not undone.success
        assert undone.error_message == "Sandbox verification failed"
        assert provider.undo_calls == 0
    finally:
        await store.shutdown()


@pytest.mark.asyncio
async def test_concurrent_store_instances_do_not_fork_audit_chain(tmp_path) -> None:
    db_path = str(tmp_path / "concurrent_action_audit.db")
    first = SQLiteActionAuditStore(db_path)
    second = SQLiteActionAuditStore(db_path)
    await first.append(
        ActionAuditEntry(
            action_id="seed",
            stage="requested",
            action_type="read_window_title",
            risk_level="readonly",
        )
    )
    try:
        await asyncio.gather(
            *[
                (first if index % 2 == 0 else second).append(
                    ActionAuditEntry(
                        action_id=f"action_{index}",
                        stage="requested",
                        action_type="read_window_title",
                        risk_level="readonly",
                    )
                )
                for index in range(20)
            ]
        )
        assert len(await first.query(limit=100)) == 21
        assert await first.verify_chain()
    finally:
        await first.shutdown()
        await second.shutdown()
