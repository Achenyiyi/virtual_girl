"""Perception Service — monitors user's environment for context awareness.

Phase 4 core component. Provides:
- Application focus tracking
- User activity state monitoring (idle, typing, gaming, meeting)
- Quiet hours enforcement
- Privacy-preserving data collection

From the PLAN:
"感知服务先本地生成低敏感事件：当前应用、窗口标题的脱敏类别、
输入/空闲状态、媒体播放、游戏状态"
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from companion.core.event_bus import EventBus
from companion.providers.perception import (
    PerceptionConfig,
    PerceptionProvider,
    PerceptionSnapshot,
)

logger = logging.getLogger(__name__)


@dataclass
class PerceptionState:
    """Current perception state, updated continuously."""

    current_app: str = ""
    current_app_category: str = "unknown"
    is_fullscreen: bool = False
    is_idle: bool = True
    idle_duration_seconds: float = 0.0
    last_input_time: float = 0.0
    is_typing: bool = False
    is_in_meeting: bool = False
    media_playing: bool = False
    media_type: str = ""

    # History for pattern detection
    app_usage_history: list[tuple[str, float]] = field(default_factory=list)  # (app, duration)
    last_updated: float = 0.0


@dataclass
class ContextAssessment:
    """Assessment of the user's current context for proactive decisions."""

    is_available: bool = True  # User is available for interaction
    is_focused: bool = True  # User is focused (not in deep work)
    is_social: bool = False  # User is in a social context
    recommended_proactive_level: int = 0
    interruption_cost: float = 0.0
    reasoning: str = ""


class PerceptionService:
    """Monitors the user's environment for contextual awareness.

    Updates the PolicyGate with current context for proactive decisions.
    """

    def __init__(
        self,
        provider: PerceptionProvider | None = None,
        bus: EventBus | None = None,
        config: PerceptionConfig | None = None,
    ) -> None:
        self._provider = provider
        self._bus = bus
        self._config = config or PerceptionConfig()
        self._state = PerceptionState()
        self._running = False
        self._poll_task: asyncio.Task[None] | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start perception monitoring."""
        if self._provider:
            await self._provider.start(self._config)
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("Perception service started")

    async def stop(self) -> None:
        """Stop perception monitoring."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
        if self._provider:
            await self._provider.stop()
        logger.info("Perception service stopped")

    async def _poll_loop(self) -> None:
        """Background polling loop for environment sensing."""
        while self._running:
            try:
                if self._provider:
                    snapshot = await self._provider.get_snapshot()
                    # Publish context events BEFORE updating state so
                    # app-switch detection compares old vs new correctly
                    if self._bus:
                        await self._publish_context_events(snapshot)
                    self._update_state(snapshot)
            except Exception:
                logger.exception("Perception poll error")

            # Sleep based on configured poll interval
            poll_ms = max(500, self._config.app_focus_poll_ms)
            await asyncio.sleep(poll_ms / 1000)

    # ── Context Assessment ────────────────────────────────────────────

    def assess_context(self) -> ContextAssessment:
        """Assess the user's current context for proactive behavior decisions.

        Returns a ContextAssessment that the PolicyGate can use to
        determine appropriate proactive behavior levels.
        """
        s = self._state
        # Idle detection
        is_idle = s.idle_duration_seconds > 30 if s.idle_duration_seconds > 0 else False
        is_away = s.idle_duration_seconds > 300 if s.idle_duration_seconds > 0 else False

        # Availability
        is_available = not s.is_in_meeting and not s.is_fullscreen
        if s.is_fullscreen:
            is_available = False  # Gaming/movies = not available

        # Focus detection
        focus_apps = {"ide", "editor", "office", "terminal"}
        is_focused = s.current_app_category not in focus_apps or not s.is_typing

        # Social context
        is_social = s.is_in_meeting or s.current_app_category in {"chat", "video_call"}

        # Interruption cost
        interruption_cost = 0.0
        if s.is_in_meeting:
            interruption_cost += 0.8
        if s.is_fullscreen:
            interruption_cost += 0.5
        if s.is_typing:
            interruption_cost += 0.3
        if is_away:
            interruption_cost += 0.1
        interruption_cost = min(1.0, interruption_cost)

        # Recommended proactive level
        if not is_available or interruption_cost > 0.7:
            level = 0  # Idle only
        elif is_away:
            level = 1  # Subtle (e.g., expression change)
        elif is_social:
            level = 0  # Don't interrupt social interactions
        elif is_idle:
            level = 2  # Hint level
        elif not is_focused:
            level = 2  # User might welcome a break
        else:
            level = 1  # Subtle presence

        reasoning_parts = []
        if not is_available:
            reasoning_parts.append("用户不可用（全屏/会议中）")
        if interruption_cost > 0.5:
            reasoning_parts.append(f"打扰成本较高({interruption_cost:.2f})")
        if is_away:
            reasoning_parts.append("用户离开")
        if is_social:
            reasoning_parts.append("社交环境")

        return ContextAssessment(
            is_available=is_available,
            is_focused=is_focused,
            is_social=is_social,
            recommended_proactive_level=level,
            interruption_cost=interruption_cost,
            reasoning="; ".join(reasoning_parts) if reasoning_parts else "可用",
        )

    # ── State management ──────────────────────────────────────────────

    def _update_state(self, snapshot: PerceptionSnapshot) -> None:
        """Update internal state from a perception snapshot."""
        now = time.time()

        self._state.current_app = snapshot.screen.app_name
        self._state.current_app_category = snapshot.screen.app_category
        self._state.is_fullscreen = snapshot.screen.is_fullscreen
        self._state.is_idle = snapshot.activity.state in ("idle", "away")
        self._state.idle_duration_seconds = snapshot.activity.idle_duration_ms / 1000.0
        self._state.is_typing = snapshot.activity.state == "active_typing"
        self._state.is_in_meeting = snapshot.activity.is_in_meeting
        self._state.last_updated = now

    async def _publish_context_events(self, snapshot: PerceptionSnapshot) -> None:
        """Publish relevant context events to the event bus."""
        if not self._bus:
            return

        # Detect app switches
        if snapshot.screen.app_name and snapshot.screen.app_name != self._state.current_app:
            from companion.events.perception import AppFocusChangedEvent

            app_event = AppFocusChangedEvent(
                app_name=snapshot.screen.app_name,
                app_category=snapshot.screen.app_category,
                window_title_deidentified=snapshot.screen.window_title_deidentified,
            )
            await self._bus.publish(app_event)

        # Detect user activity state changes
        if snapshot.activity.state in ("idle", "away") and not self._state.is_idle:
            from companion.events.perception import UserActivityStateChangedEvent

            activity_event = UserActivityStateChangedEvent(
                state="idle",
                idle_duration_ms=snapshot.activity.idle_duration_ms,
            )
            await self._bus.publish(activity_event)

    # ── Query methods ─────────────────────────────────────────────────

    def get_current_app(self) -> str:
        return self._state.current_app

    def is_user_available(self) -> bool:
        return self.assess_context().is_available

    def is_quiet_hours(self) -> bool:
        """Check if current time falls in quiet hours."""
        now = datetime.now()
        hour = now.hour
        start = self._config.quiet_hours_start  # "23:00"
        end = self._config.quiet_hours_end  # "07:00"
        try:
            start_h = int(start.split(":")[0])
            end_h = int(end.split(":")[0])
            if start_h > end_h:
                # Wraps midnight
                return hour >= start_h or hour < end_h
            return start_h <= hour < end_h
        except (ValueError, IndexError):
            return hour >= 23 or hour < 7

    def get_state_summary(self) -> dict[str, Any]:
        return {
            "app": self._state.current_app,
            "category": self._state.current_app_category,
            "is_fullscreen": self._state.is_fullscreen,
            "is_idle": self._state.is_idle,
            "idle_seconds": self._state.idle_duration_seconds,
            "is_typing": self._state.is_typing,
            "is_in_meeting": self._state.is_in_meeting,
            "quiet_hours": self.is_quiet_hours(),
        }
