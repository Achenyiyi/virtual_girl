"""Windows production-boundary tests for capability-confined read-only actions."""

from __future__ import annotations

import sys

import pytest

from companion.__main__ import CompanionApp
from companion.config_loader import RuntimeConfig
from companion.core.policy_gate import PolicyGate
from companion.providers.action import ActionRequest
from companion.providers.base import ProviderHealth
from companion.providers.implementations.windows_readonly_action import (
    WindowsReadOnlyActionConfig,
    WindowsReadOnlyActionProvider,
)
from companion.security.action_audit import SQLiteActionAuditStore
from companion.services.action_service import ActionService, ActionServiceConfig

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows provider")


@pytest.mark.asyncio
async def test_real_windows_readonly_action_is_sandboxed_and_audited(tmp_path) -> None:
    provider = WindowsReadOnlyActionProvider()
    store = SQLiteActionAuditStore(str(tmp_path / "windows_action_audit.db"))
    service = ActionService(provider=provider, policy_gate=PolicyGate(), audit_store=store)
    try:
        assert await provider.health_check() == ProviderHealth.HEALTHY

        record, result = await service.request("check_system_status")

        assert result is not None and result.success
        assert record.request.sandbox_id == "windows-readonly-allowlist-v1"
        assert result.result_data is not None
        assert result.result_data["cpu_count"] >= 1
        assert result.result_data["memory_total_bytes"] > 0
        assert result.result_data["disk_total_bytes"] > 0
        assert await store.verify_chain()
        entries = await store.query(action_id=record.request.action_id)
        assert {entry.stage for entry in entries} == {"requested", "executing", "executed"}

        for action_type, method in [
            ("read_window_title", "uia"),
            ("read_active_app", "uia"),
        ]:
            direct_result = await provider.execute(
                ActionRequest(
                    action_id=f"direct-{action_type}",
                    action_type=action_type,
                    method=method,
                    risk_level="readonly",
                    sandbox_id="windows-readonly-allowlist-v1",
                )
            )
            assert direct_result.success
            assert direct_result.result_data is not None
            assert "window_available" in direct_result.result_data
    finally:
        await provider.shutdown()
        await store.shutdown()


@pytest.mark.asyncio
async def test_mutating_action_cannot_cross_readonly_provider_boundary(tmp_path) -> None:
    class CountingProvider(WindowsReadOnlyActionProvider):
        def __init__(self) -> None:
            super().__init__()
            self.execute_calls = 0

        async def execute(self, request):
            self.execute_calls += 1
            return await super().execute(request)

    provider = CountingProvider()
    store = SQLiteActionAuditStore(str(tmp_path / "windows_action_audit.db"))
    service = ActionService(provider=provider, policy_gate=PolicyGate(), audit_store=store)
    try:
        record, result = await service.request("open_app")

        assert result is not None and not result.success
        assert result.error_message == "Sandbox verification failed"
        assert record.state.value == "failed"
        assert provider.execute_calls == 0
        assert await store.verify_chain()
    finally:
        await provider.shutdown()
        await store.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action_request",
    [
        ActionRequest("a1", "read_window_title", "uia", {"unexpected": True}, "readonly"),
        ActionRequest("a2", "read_window_title", "api", {}, "readonly"),
        ActionRequest("a3", "read_window_title", "uia", {}, "irreversible"),
        ActionRequest("a4", "delete_file", "api", {}, "readonly"),
    ],
)
async def test_provider_rejects_tampered_requests(action_request) -> None:
    provider = WindowsReadOnlyActionProvider()
    try:
        sandbox = await provider.verify_sandbox(action_request)
        result = await provider.execute(action_request)

        assert not sandbox.verified
        assert not result.success
        assert result.result_data is None
    finally:
        await provider.shutdown()


@pytest.mark.asyncio
async def test_provider_permissions_preview_and_shutdown_are_fail_closed() -> None:
    provider = WindowsReadOnlyActionProvider()
    without_sandbox = ActionRequest(
        "unverified", "check_system_status", "api", risk_level="readonly"
    )
    valid = ActionRequest(
        "status",
        "check_system_status",
        "api",
        risk_level="readonly",
        sandbox_id="windows-readonly-allowlist-v1",
    )

    sandbox = await provider.verify_sandbox(without_sandbox)
    unverified_result = await provider.execute(without_sandbox)
    permissions = await provider.get_permissions()
    preview = await provider.preview(valid)
    undo = await provider.undo("status")

    assert sandbox.verified
    assert not unverified_result.success
    assert "verified sandbox" in (unverified_result.error_message or "")
    assert {permission.action_pattern for permission in permissions} == {
        "check_system_status",
        "read_active_app",
        "read_window_title",
    }
    assert all(permission.auto_approve for permission in permissions)
    assert preview["side_effects"] == "none"
    assert not undo.success
    assert await provider.get_audit_log() == []
    with pytest.raises(PermissionError, match="immutable"):
        await provider.update_permissions([])

    await provider.shutdown()

    assert await provider.health_check() == ProviderHealth.UNHEALTHY
    result = await provider.execute(valid)
    assert not result.success
    assert "shut down" in (result.error_message or "")


@pytest.mark.asyncio
async def test_companion_app_wires_action_service_and_closes_audit(tmp_path) -> None:
    audit_path = tmp_path / "app_action_audit.db"
    app = CompanionApp(
        RuntimeConfig(
            action_provider_config=None,
            action_service_config=None,
        )
    )
    assert app.action_service is None
    await app.stop()

    app = CompanionApp(
        RuntimeConfig(
            action_provider_config=WindowsReadOnlyActionConfig(),
            action_service_config=ActionServiceConfig(
                sandbox_enabled=True,
                audit_enabled=True,
                require_durable_audit=True,
                allow_reversible_low_auto=False,
            ),
            action_audit_db_path=str(audit_path),
        )
    )
    assert app.action_service is not None
    record, result = await app.action_service.request("check_system_status")
    assert result is not None and result.success
    await app.stop()

    reopened = SQLiteActionAuditStore(str(audit_path))
    try:
        assert await reopened.verify_chain()
        assert await reopened.query(action_id=record.request.action_id)
    finally:
        await reopened.shutdown()
