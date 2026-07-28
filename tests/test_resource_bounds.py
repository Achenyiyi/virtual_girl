from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

import pytest

from companion.__main__ import setup_logging
from companion.core.event_bus import EventBus
from companion.core.policy_gate import PolicyGate, PolicyGateConfig, ProactiveLevel
from companion.events.conversation import ConversationTurnStartedEvent
from companion.services.action_service import ActionService, ActionServiceConfig
from companion.services.proactive_scheduler import (
    ProactiveScheduler,
    ProactiveTrigger,
    SchedulerConfig,
)


def test_file_logging_rotates_and_limits_backup_count(tmp_path) -> None:
    log_path = tmp_path / "companion.log"
    setup_logging("INFO", str(log_path), max_bytes=1024, backup_count=2)
    logger = logging.getLogger("resource-bound-test")

    for index in range(200):
        logger.info("record %s %s", index, "x" * 100)
    logging.shutdown()

    files = sorted(tmp_path.glob("companion.log*"))
    assert log_path in files
    assert len(files) <= 3
    assert all(path.stat().st_size < 2048 for path in files)


@pytest.mark.asyncio
async def test_event_bus_replay_cache_is_bounded() -> None:
    bus = EventBus("bounded", max_log_size=3)
    for sequence in range(5):
        await bus.publish(
            ConversationTurnStartedEvent(
                session_id="session",
                turn_id=f"turn-{sequence}",
                turn_sequence=sequence,
            )
        )

    turn_ids: list[str] = []
    await bus.replay(lambda event: turn_ids.append(event.turn_id))  # type: ignore[attr-defined]

    assert turn_ids == ["turn-2", "turn-3", "turn-4"]


@pytest.mark.asyncio
async def test_action_history_and_in_memory_audit_are_bounded() -> None:
    service = ActionService(
        config=ActionServiceConfig(
            sandbox_enabled=False,
            audit_enabled=True,
            require_durable_audit=False,
            max_history_entries=3,
            max_in_memory_audit_entries=4,
        )
    )

    for _ in range(8):
        await service.request("read_window_title")

    assert len(service.get_recent_actions(limit=100)) == 3
    assert len(service.get_audit_log(limit=100)) == 4


@pytest.mark.asyncio
async def test_pending_confirmation_capacity_is_bounded() -> None:
    service = ActionService(
        config=ActionServiceConfig(
            sandbox_enabled=False,
            audit_enabled=False,
            require_durable_audit=False,
            max_pending_confirmations=2,
        )
    )

    first, first_result = await service.request("send_message")
    second, second_result = await service.request("send_message")
    rejected, rejected_result = await service.request("send_message")

    assert first_result is None and second_result is None
    assert rejected_result is not None and not rejected_result.success
    assert rejected.state.value == "denied"
    assert len(service.get_pending_actions()) == 2
    assert {first.request.action_id, second.request.action_id} == {
        record.request.action_id for record in service.get_pending_actions()
    }


@pytest.mark.asyncio
async def test_pending_confirmation_capacity_is_atomic_under_concurrency() -> None:
    service = ActionService(
        config=ActionServiceConfig(
            sandbox_enabled=False,
            audit_enabled=False,
            require_durable_audit=False,
            max_pending_confirmations=3,
        )
    )

    results = await asyncio.gather(*(service.request("send_message") for _ in range(20)))

    assert len(service.get_pending_actions()) == 3
    assert sum(result is None for _, result in results) == 3


@pytest.mark.asyncio
async def test_expired_confirmation_is_revoked_and_cannot_execute() -> None:
    service = ActionService(
        config=ActionServiceConfig(
            sandbox_enabled=False,
            audit_enabled=False,
            require_durable_audit=False,
            confirmation_ttl_seconds=1,
        )
    )
    record, result = await service.request("send_message")
    assert result is None
    record.created_at = time.time() - 2

    assert await service.confirm(record.request.action_id, approved=True) is None
    assert record.state.value == "revoked"
    assert service.get_pending_actions() == []


@pytest.mark.asyncio
async def test_proactive_scheduler_feedback_history_is_bounded() -> None:
    policy = PolicyGate(
        PolicyGateConfig(
            quiet_hours_enabled=False,
            level_1_per_hour=100,
            level_1_cooldown_seconds=0,
        )
    )
    scheduler = ProactiveScheduler(
        policy,
        config=SchedulerConfig(feedback_window_size=3),
    )

    for _ in range(8):
        await scheduler.check_and_schedule(
            ProactiveTrigger.PERIODIC_CHECK,
            relevance=1.0,
        )

    assert len(scheduler._history) == 3


def test_policy_gate_discards_expired_proactive_history() -> None:
    gate = PolicyGate(PolicyGateConfig(quiet_hours_enabled=False))
    gate._proactive_history = deque(
        [
            (time.time() - 7200, ProactiveLevel.LEVEL_1_SUBTLE),
            (time.time(), ProactiveLevel.LEVEL_1_SUBTLE),
        ]
    )

    assert gate._count_recent_proactives(ProactiveLevel.LEVEL_1_SUBTLE, 3600) == 1
    assert len(gate._proactive_history) == 1


@pytest.mark.parametrize(
    "config",
    [
        ActionServiceConfig,
        lambda: SchedulerConfig(periodic_check_interval_seconds=0),
        lambda: SchedulerConfig(feedback_window_size=0),
    ],
)
def test_resource_configs_validate_bounds(config) -> None:
    if config is ActionServiceConfig:
        with pytest.raises(ValueError):
            ActionServiceConfig(max_history_entries=0)
    else:
        with pytest.raises(ValueError):
            config()
