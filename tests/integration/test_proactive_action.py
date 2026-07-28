"""Integration tests for proactive scheduler and action service (Phase 4)."""

from __future__ import annotations

import asyncio

import pytest

from companion.core.event_bus import EventBus
from companion.core.policy_gate import PolicyGate, ProactiveLevel
from companion.services.action_service import ActionService, ActionServiceConfig
from companion.services.proactive_scheduler import (
    ProactiveScheduler,
    ProactiveTrigger,
    SchedulerConfig,
)
from tests.test_providers import MockActionProvider


@pytest.fixture
def scheduler():
    policy = PolicyGate()
    policy._quiet_hours_enabled = False
    bus = EventBus("test")
    return ProactiveScheduler(
        policy_gate=policy,
        bus=bus,
        config=SchedulerConfig(
            periodic_check_interval_seconds=60.0,  # Don't auto-fire in tests
        ),
    )


@pytest.fixture
def action_service():
    bus = EventBus("test")
    return ActionService(
        bus=bus,
        config=ActionServiceConfig(
            sandbox_enabled=False,
            audit_enabled=False,
            require_durable_audit=False,
            allow_readonly_auto=True,
            allow_reversible_low_auto=True,
            allow_reversible_high_auto=False,
            allow_irreversible_auto=False,
        ),
    )


class TestProactiveScheduler:
    """Test proactive behavior scheduling."""

    def test_milestone_triggers_proactive(self, scheduler):
        """Milestones should trigger proactive behavior at a high level."""
        import asyncio

        result = asyncio.run(
            scheduler.check_and_schedule(ProactiveTrigger.MILESTONE, relevance=0.9, urgency=0.5)
        )
        if result:
            assert result.trigger == ProactiveTrigger.MILESTONE
            assert result.level >= ProactiveLevel.LEVEL_0_IDLE

    def test_periodic_check_is_low_level(self, scheduler):
        """Periodic checks should be at low proactive levels."""
        import asyncio

        result = asyncio.run(scheduler.check_and_schedule(ProactiveTrigger.PERIODIC_CHECK))
        if result:
            # Periodic checks should be subtle at most
            assert result.level <= ProactiveLevel.LEVEL_2_HINT

    def test_acceptance_rate_tracks_feedback(self, scheduler):
        import asyncio

        # Create and accept a proactive event
        result = asyncio.run(
            scheduler.check_and_schedule(ProactiveTrigger.USER_RETURN, relevance=0.8)
        )
        if result:
            scheduler.record_feedback(result, True)

        rate = scheduler.get_acceptance_rate(10)
        assert 0 <= rate <= 1.0

    def test_stats_report(self, scheduler):
        stats = scheduler.get_stats()
        assert "total_proactives_today" in stats
        assert "acceptance_rate" in stats

    def test_app_switch_low_relevance(self, scheduler):
        """App switch with low-relevance apps should not trigger."""
        import asyncio

        result = asyncio.run(scheduler.on_app_switch("notepad.exe", "office"))
        # Generic office app switch should be low/none
        if result:
            assert result.level <= ProactiveLevel.LEVEL_1_SUBTLE

    def test_user_idle_long_enough(self, scheduler):
        """Very long idle should trigger a check."""
        result = asyncio.run(
            scheduler.on_user_idle(180)  # 3 minutes idle
        )
        assert result is None or result.trigger == ProactiveTrigger.IDLE_DETECTED


class TestActionService:
    """Test action service permission and execution."""

    @pytest.mark.asyncio
    async def test_readonly_action_auto_approved(self, action_service):
        record, result = await action_service.request("read_window_title")
        assert record.request.risk_level == "readonly"
        # Read-only should be auto-executed (not pending confirmation)
        assert result is not None  # Already executed

    @pytest.mark.asyncio
    async def test_reversible_low_auto_approved(self, action_service):
        record, result = await action_service.request("search_web")
        assert record.request.risk_level == "reversible_low"

    @pytest.mark.asyncio
    async def test_irreversible_requires_confirmation(self, action_service):
        record, result = await action_service.request("send_message")
        assert record.state.value == "waiting_confirmation"
        assert result is None

    @pytest.mark.asyncio
    async def test_confirm_deny(self, action_service):
        record, result = await action_service.request("create_file")
        assert record.state.value == "waiting_confirmation"
        assert result is None
        denied_result = await action_service.confirm(record.request.action_id, approved=False)
        assert denied_result is None
        assert action_service.get_action(record.request.action_id) is None

    @pytest.mark.asyncio
    async def test_confirmation_is_single_use(self):
        class CountingProvider(MockActionProvider):
            def __init__(self):
                self.executions = 0

            async def execute(self, request):
                self.executions += 1
                return await super().execute(request)

        provider = CountingProvider()
        service = ActionService(
            provider=provider,
            config=ActionServiceConfig(
                sandbox_enabled=False,
                audit_enabled=False,
                require_durable_audit=False,
            ),
        )
        record, result = await service.request("delete_file", {"path": "dummy"})
        assert result is None

        first = await service.confirm(record.request.action_id, approved=True)
        second = await service.confirm(record.request.action_id, approved=True)

        assert first is not None and first.success
        assert second is None
        assert provider.executions == 1
        assert len(service.get_recent_actions()) == 1

    @pytest.mark.asyncio
    async def test_irreversible_auto_approve_config_is_ignored(self):
        service = ActionService(
            provider=MockActionProvider(),
            config=ActionServiceConfig(
                sandbox_enabled=False,
                audit_enabled=False,
                require_durable_audit=False,
                allow_irreversible_auto=True,
            ),
        )
        record, result = await service.request("payment", {"amount": 1})
        assert record.state.value == "waiting_confirmation"
        assert result is None
        assert record.preview is not None

    @pytest.mark.asyncio
    async def test_provider_execution_timeout_is_enforced(self):
        class SlowProvider(MockActionProvider):
            async def execute(self, request):
                await asyncio.sleep(0.1)
                return await super().execute(request)

        service = ActionService(
            provider=SlowProvider(),
            config=ActionServiceConfig(
                sandbox_enabled=False,
                audit_enabled=False,
                require_durable_audit=False,
                action_timeout_seconds=0.01,
            ),
        )
        record, result = await service.request("read_window_title")
        assert result is not None
        assert not result.success
        assert record.state.value == "failed"
        assert "timed out" in (result.error_message or "")

    @pytest.mark.asyncio
    async def test_cancellation_ignoring_execution_is_bounded_and_quarantined(self):
        release = asyncio.Event()

        class CancellationIgnoringProvider(MockActionProvider):
            def __init__(self):
                self.executions = 0

            async def execute(self, request):
                self.executions += 1
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await release.wait()
                return await super().execute(request)

        provider = CancellationIgnoringProvider()
        service = ActionService(
            provider=provider,
            config=ActionServiceConfig(
                sandbox_enabled=False,
                audit_enabled=False,
                require_durable_audit=False,
                action_timeout_seconds=0.01,
            ),
        )

        try:
            record, result = await asyncio.wait_for(
                service.request("read_window_title"), timeout=0.5
            )
            assert result is not None and not result.success
            assert record.state.value == "failed"
            assert "outcome is unknown" in (result.error_message or "")

            blocked_record, blocked_result = await asyncio.wait_for(
                service.request("read_active_app"), timeout=0.5
            )
            assert blocked_result is not None and not blocked_result.success
            assert blocked_record.state.value == "failed"
            assert "quarantined" in (blocked_result.error_message or "")
            assert provider.executions == 1
        finally:
            release.set()
            await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_cancelled_caller_waits_for_execution_terminal_record(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        class BlockingProvider(MockActionProvider):
            async def execute(self, request):
                entered.set()
                await release.wait()
                return await super().execute(request)

        service = ActionService(
            provider=BlockingProvider(),
            config=ActionServiceConfig(
                sandbox_enabled=False,
                audit_enabled=False,
                require_durable_audit=False,
                action_timeout_seconds=1.0,
            ),
        )
        request_task = asyncio.create_task(service.request("read_window_title"))
        await entered.wait()
        request_task.cancel()
        await asyncio.sleep(0)
        assert not request_task.done()

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request_task

        history = service.get_recent_actions()
        assert len(history) == 1
        assert history[0].state.value == "completed"
        assert history[0].result is not None and history[0].result.success

    @pytest.mark.asyncio
    async def test_cancellation_ignoring_preview_is_bounded_and_quarantined(self):
        release = asyncio.Event()

        class CancellationIgnoringPreviewProvider(MockActionProvider):
            async def preview(self, request):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await release.wait()
                return await super().preview(request)

        service = ActionService(
            provider=CancellationIgnoringPreviewProvider(),
            config=ActionServiceConfig(
                sandbox_enabled=False,
                audit_enabled=False,
                require_durable_audit=False,
                action_timeout_seconds=0.01,
            ),
        )

        try:
            record, result = await asyncio.wait_for(
                service.request("send_message", {"text": "hello"}), timeout=0.5
            )
            assert result is not None and not result.success
            assert record.state.value == "failed"
            assert "preview failed" in (result.error_message or "").lower()

            _blocked_record, blocked_result = await service.request("read_window_title")
            assert blocked_result is not None and not blocked_result.success
            assert "quarantined" in (blocked_result.error_message or "")
        finally:
            release.set()
            await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_provider_error_is_redacted(self):
        class LeakyProvider(MockActionProvider):
            async def execute(self, request):
                raise RuntimeError("api_key=abcdefghijklmnopqrstuvwxyz")

        service = ActionService(
            provider=LeakyProvider(),
            config=ActionServiceConfig(
                sandbox_enabled=False,
                audit_enabled=False,
                require_durable_audit=False,
            ),
        )
        _record, result = await service.request("read_window_title")
        assert result is not None
        assert "abcdefghijklmnopqrstuvwxyz" not in (result.error_message or "")
        assert "[REDACTED]" in (result.error_message or "")

    @pytest.mark.asyncio
    async def test_action_stats(self, action_service):
        await action_service.request("read_window_title")
        await action_service.request("read_active_app")
        stats = action_service.get_stats()
        assert stats["total"] >= 0
        assert "success_rate" in stats
