"""Proactive Scheduler — decides when and how the companion initiates interaction.

Phase 4 core component. Implements the five-level proactive hierarchy:

Level 0: idle animations (breathing, blinking, posture)
Level 1: subtle (facial expression, silent bubble)
Level 2: hint (short text, one-click dismiss)
Level 3: conversation (active speech, high-value + low-interruption)
Level 4: action proposal (suggest computer action, must respect permissions)

Key design from the PLAN:
- Deterministic utility function, not LLM judgment
- Quiet hours, cooldowns, daily budgets
- Context-aware: respects meetings, fullscreen, typing intensity
- User feedback loop: rejections → reduced proactivity
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from companion.core.event_bus import EventBus
from companion.core.policy_gate import PolicyGate, ProactiveDecision, ProactiveLevel

logger = logging.getLogger(__name__)


class ProactiveTrigger(IntEnum):
    """What triggered a proactive behavior check."""

    APP_SWITCH = 1
    IDLE_DETECTED = 2
    MILESTONE = 3
    SCHEDULE_EVENT = 4
    REFLECTION_PLAN = 5
    USER_RETURN = 6
    PERIODIC_CHECK = 7


@dataclass
class ProactiveEvent:
    """A proactive behavior that was executed."""

    trigger: ProactiveTrigger
    level: ProactiveLevel
    timestamp: float
    decision: ProactiveDecision
    content: str = ""
    user_accepted: bool | None = None  # None = no feedback yet


@dataclass
class SchedulerConfig:
    """Configuration for the proactive scheduler."""

    periodic_check_interval_seconds: float = 30.0
    max_pending_proposals: int = 3
    feedback_window_size: int = 50
    learning_rate: float = 0.1

    def __post_init__(self) -> None:
        if self.periodic_check_interval_seconds <= 0:
            raise ValueError("periodic_check_interval_seconds must be positive")
        if self.feedback_window_size < 1:
            raise ValueError("feedback_window_size must be positive")


class ProactiveScheduler:
    """Schedules and manages proactive companion behaviors.

    The scheduler is the bridge between:
    - Perception: "user switched to browser"
    - PolicyGate: "is it OK to say something?"
    - ExpressionMapper: "how should we look when saying it?"
    - Orchestrator: "generate a contextual message"
    """

    def __init__(
        self,
        policy_gate: PolicyGate,
        bus: EventBus | None = None,
        config: SchedulerConfig | None = None,
    ) -> None:
        self._policy = policy_gate
        self._bus = bus
        self._config = config or SchedulerConfig()

        self._history: deque[ProactiveEvent] = deque(maxlen=self._config.feedback_window_size)
        self._running = False
        self._check_task: asyncio.Task[None] | None = None

        # State
        self._last_app: str = ""
        self._was_idle: bool = False

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._check_task = asyncio.create_task(self._periodic_check_loop())
        logger.info("Proactive scheduler started")

    async def stop(self) -> None:
        self._running = False
        if self._check_task:
            self._check_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._check_task
        logger.info("Proactive scheduler stopped")

    async def _periodic_check_loop(self) -> None:
        """Background loop for periodic proactive checks."""
        while self._running:
            try:
                await self.check_and_schedule(ProactiveTrigger.PERIODIC_CHECK)
            except Exception:
                logger.exception("Proactive check error")
            await asyncio.sleep(self._config.periodic_check_interval_seconds)

    # ── Trigger handlers ──────────────────────────────────────────────

    async def on_app_switch(self, app_name: str, app_category: str) -> ProactiveEvent | None:
        """Called when the user switches to a different application."""
        if app_name == self._last_app:
            return None
        self._last_app = app_name

        # Only trigger on certain categories
        trigger_categories = {"browser", "media", "game"}
        if app_category in trigger_categories:
            return await self.check_and_schedule(ProactiveTrigger.APP_SWITCH)
        return None

    async def on_user_idle(self, idle_seconds: float) -> ProactiveEvent | None:
        """Called when user becomes idle."""
        if not self._was_idle and idle_seconds > 60:
            self._was_idle = True
            return await self.check_and_schedule(ProactiveTrigger.IDLE_DETECTED)
        if idle_seconds < 10:
            self._was_idle = False
        return None

    async def on_user_return(self) -> ProactiveEvent | None:
        """Called when user returns after being away."""
        if self._was_idle:
            self._was_idle = False
            return await self.check_and_schedule(ProactiveTrigger.USER_RETURN)
        return None

    async def on_milestone(self, milestone_type: str, value: str) -> ProactiveEvent | None:
        """Called when a relationship milestone is reached."""
        return await self.check_and_schedule(ProactiveTrigger.MILESTONE)

    async def on_reflection_plan(self, plan_text: str) -> ProactiveEvent | None:
        """Called when the reflection engine produces a candidate plan."""
        return await self.check_and_schedule(ProactiveTrigger.REFLECTION_PLAN)

    # ── Core decision logic ───────────────────────────────────────────

    async def check_and_schedule(
        self,
        trigger: ProactiveTrigger,
        relevance: float = 0.5,
        urgency: float = 0.0,
        relationship_value: float = 0.3,
    ) -> ProactiveEvent | None:
        """Evaluate whether to initiate a proactive behavior.

        Uses the PolicyGate for the go/no-go decision.
        """
        # Adjust relevance based on trigger type
        trigger_relevance = {
            ProactiveTrigger.MILESTONE: 0.9,
            ProactiveTrigger.USER_RETURN: 0.7,
            ProactiveTrigger.REFLECTION_PLAN: 0.6,
            ProactiveTrigger.SCHEDULE_EVENT: 0.8,
            ProactiveTrigger.IDLE_DETECTED: 0.3,
            ProactiveTrigger.APP_SWITCH: 0.4,
            ProactiveTrigger.PERIODIC_CHECK: 0.2,
        }
        relevance = max(relevance, trigger_relevance.get(trigger, 0.3))

        # Adjust urgency
        trigger_urgency = {
            ProactiveTrigger.SCHEDULE_EVENT: 0.8,
            ProactiveTrigger.MILESTONE: 0.5,
            ProactiveTrigger.REFLECTION_PLAN: 0.4,
        }
        urgency = max(urgency, trigger_urgency.get(trigger, 0.0))

        # Propose at an appropriate level based on trigger
        proposed_level = {
            ProactiveTrigger.MILESTONE: ProactiveLevel.LEVEL_3_CONVERSATION,
            ProactiveTrigger.USER_RETURN: ProactiveLevel.LEVEL_2_HINT,
            ProactiveTrigger.REFLECTION_PLAN: ProactiveLevel.LEVEL_2_HINT,
            ProactiveTrigger.SCHEDULE_EVENT: ProactiveLevel.LEVEL_3_CONVERSATION,
            ProactiveTrigger.IDLE_DETECTED: ProactiveLevel.LEVEL_1_SUBTLE,
            ProactiveTrigger.APP_SWITCH: ProactiveLevel.LEVEL_1_SUBTLE,
            ProactiveTrigger.PERIODIC_CHECK: ProactiveLevel.LEVEL_1_SUBTLE,
        }.get(trigger, ProactiveLevel.LEVEL_1_SUBTLE)

        # Evaluate through policy gate
        decision = self._policy.evaluate_proactive(
            proposed_level,
            relevance=relevance,
            urgency=urgency,
            relationship_value=relationship_value,
        )

        # Record event
        event = ProactiveEvent(
            trigger=trigger,
            level=decision.level if decision.allowed else ProactiveLevel.LEVEL_0_IDLE,
            timestamp=time.time(),
            decision=decision,
        )

        if decision.allowed:
            self._history.append(event)
            logger.info(
                "Proactive: level=%s trigger=%s utility=%.2f reason=%s",
                decision.level.name if decision.level > 0 else "blocked",
                trigger.name,
                decision.utility_score,
                decision.reason,
            )

            # Notify via event bus
            if self._bus:
                await self._notify_proactive(event)

            return event

        return None

    # ── Feedback ──────────────────────────────────────────────────────

    def record_feedback(self, event: ProactiveEvent, accepted: bool) -> None:
        """Record whether a proactive behavior was accepted or rejected."""
        event.user_accepted = accepted
        self._policy.record_feedback(accepted)

        # Adjust future behavior based on feedback
        if not accepted:
            logger.debug("Proactive rejected, adjusting policy")

    def get_acceptance_rate(self, window: int | None = None) -> float:
        """Get the recent proactive acceptance rate."""
        events = list(self._history)[-window:] if window else list(self._history)
        rated = [e for e in events if e.user_accepted is not None]
        if not rated:
            return 1.0  # No feedback yet = optimistic
        return sum(1 for e in rated if e.user_accepted) / len(rated)

    # ── Internal ──────────────────────────────────────────────────────

    async def _notify_proactive(self, event: ProactiveEvent) -> None:
        """Publish a proactive behavior event to the bus."""
        if not self._bus:
            return

        # Publish a perception event with the schedule-detected type
        # (signals proactive intent without conflating with activity state)
        from companion.events.perception import ScheduleEventDetectedEvent

        context_event = ScheduleEventDetectedEvent(
            event_category=f"proactive.level_{int(event.level)}",
            title_deidentified=f"Proactive trigger: {event.trigger.name}",
            is_upcoming=True,
        )
        await self._bus.publish(context_event)

    # ── Stats ─────────────────────────────────────────────────────────

    def get_stats(self) -> dict[str, Any]:
        now = time.time()
        hour_ago = now - 3600
        recent = [e for e in self._history if e.timestamp > hour_ago]
        by_level: dict[str, int] = {}
        for e in recent:
            level_name = e.level.name
            by_level[level_name] = by_level.get(level_name, 0) + 1

        return {
            "total_proactives_today": len(self._history),
            "proactives_last_hour": len(recent),
            "by_level": by_level,
            "acceptance_rate": self.get_acceptance_rate(20),
            "acceptance_rate_all": self.get_acceptance_rate(),
        }
