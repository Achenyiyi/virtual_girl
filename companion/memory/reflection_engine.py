"""Reflection Engine — synthesizes insights from accumulated memories.

Implements Layer 5 of the five-layer memory system.
Based on Generative Agents (Park et al., 2023) — observations accumulate
until importance threshold triggers reflection generation.

Key design from the PLAN:
- Async, non-blocking — never waits in the fast dialogue loop
- Daily budget — limited number of reflections per day
- Always cites source events
- Can generate candidate plans for proactive behavior
- Cannot modify identity core or safety boundaries
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from companion.events.base import generate_ulid
from companion.providers.memory import Reflection

logger = logging.getLogger(__name__)


@dataclass
class ReflectionConfig:
    """Configuration for the reflection engine."""

    # How many significant events before triggering a reflection
    importance_threshold: float = 0.7
    min_events_for_reflection: int = 5

    # Budget
    max_reflections_per_day: int = 10
    max_reflections_per_hour: int = 2

    # Timing
    min_interval_seconds: int = 600  # 10 min between reflections

    # Categories to reflect on
    enabled_categories: list[str] = field(
        default_factory=lambda: [
            "relationship_insight",
            "behavior_pattern",
            "goal_tracking",
            "preference_summary",
        ]
    )


@dataclass
class ReflectionCandidate:
    """A candidate for reflection generation."""

    category: str
    source_event_ids: list[str]
    source_episode_ids: list[str]
    accumulated_importance: float = 0.0
    events_since_last_reflection: int = 0

    def should_reflect(self, config: ReflectionConfig) -> bool:
        return (
            self.accumulated_importance >= config.importance_threshold
            and self.events_since_last_reflection >= config.min_events_for_reflection
        )


class ReflectionEngine:
    """Periodically synthesizes insights from accumulated events.

    Usage pattern (from the PLAN):
    1. Each significant event feeds into the reflection engine
    2. When importance crosses threshold, a reflection is generated
    3. Reflections are generated asynchronously (slow loop)
    4. Reflections can propose candidate plans for proactive behavior
    """

    def __init__(self, config: ReflectionConfig | None = None) -> None:
        self._config = config or ReflectionConfig()
        self._candidates: dict[str, ReflectionCandidate] = self._initialize_candidates()
        self._generated_reflections: list[Reflection] = []
        self._daily_count: int = 0
        self._last_reflection_time: datetime | None = None
        self._daily_reset_date: str = ""

    def _initialize_candidates(self) -> dict[str, ReflectionCandidate]:
        """Initialize tracking for each reflection category."""
        return {
            cat: ReflectionCandidate(category=cat, source_event_ids=[], source_episode_ids=[])
            for cat in self._config.enabled_categories
        }

    def feed_event(
        self,
        event_id: str,
        category_hint: str | None = None,
        importance: float = 0.3,
    ) -> Reflection | None:
        """Feed an event into the engine. Returns a reflection if triggered.

        Args:
            event_id: The event that occurred
            category_hint: Which reflection category this relates to
            importance: How significant this event is (0-1)

        Returns:
            A new Reflection if the threshold was crossed, None otherwise
        """
        self._check_daily_reset()

        # Determine which categories this event feeds
        target_categories = (
            [category_hint]
            if category_hint and category_hint in self._candidates
            else self._pick_categories(event_id, importance)
        )

        triggered: Reflection | None = None

        for cat in target_categories:
            candidate = self._candidates[cat]
            candidate.source_event_ids.append(event_id)
            candidate.accumulated_importance += importance
            candidate.events_since_last_reflection += 1

            if candidate.should_reflect(self._config) and self._can_generate():
                reflection = self._generate_reflection(cat, candidate)
                if reflection:
                    self._generated_reflections.append(reflection)
                    self._daily_count += 1
                    self._last_reflection_time = datetime.now(UTC)
                    # Reset candidate
                    candidate.accumulated_importance = 0.0
                    candidate.events_since_last_reflection = 0
                    triggered = reflection

        return triggered

    def feed_episode(self, episode_id: str, salience: float) -> None:
        """Feed an episode completion into the reflection engine."""
        # Episodes feed into relationship_insight and behavior_pattern categories
        if "relationship_insight" in self._candidates:
            self._candidates["relationship_insight"].source_episode_ids.append(episode_id)
            self._candidates["relationship_insight"].accumulated_importance += salience * 0.5

        if "behavior_pattern" in self._candidates:
            self._candidates["behavior_pattern"].source_episode_ids.append(episode_id)
            self._candidates["behavior_pattern"].accumulated_importance += salience * 0.3

    def get_pending_plans(self) -> list[str]:
        """Get candidate plans from recent reflections."""
        plans = []
        for r in self._generated_reflections[-5:]:  # Last 5 reflections
            if r.generated_plan:
                plans.append(r.generated_plan)
        return plans

    def force_reflection(self, category: str) -> Reflection | None:
        """Force-generate a reflection for a category (for testing)."""
        if category not in self._candidates:
            return None
        candidate = self._candidates[category]
        reflection = self._generate_reflection(category, candidate)
        if reflection:
            self._generated_reflections.append(reflection)
            candidate.accumulated_importance = 0.0
            candidate.events_since_last_reflection = 0
        return reflection

    # ── Internal ──────────────────────────────────────────────────────

    def _check_daily_reset(self) -> None:
        """Reset daily counter when the date changes."""
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if today != self._daily_reset_date:
            self._daily_count = 0
            self._daily_reset_date = today
            self._candidates = self._initialize_candidates()

    def _can_generate(self) -> bool:
        """Check if we're within budget to generate a reflection."""
        if self._daily_count >= self._config.max_reflections_per_day:
            logger.debug("Reflection daily budget exhausted")
            return False

        # Check hourly budget
        hour_ago = datetime.now(UTC)
        recent = [
            r
            for r in self._generated_reflections
            if r.created_at and (hour_ago - r.created_at).total_seconds() < 3600
        ]
        if len(recent) >= self._config.max_reflections_per_hour:
            logger.debug("Reflection hourly budget exhausted")
            return False

        # Check minimum interval
        if self._last_reflection_time:
            elapsed = (datetime.now(UTC) - self._last_reflection_time).total_seconds()
            if elapsed < self._config.min_interval_seconds:
                logger.debug("Reflection interval too short: %.0fs", elapsed)
                return False

        return True

    def _pick_categories(self, event_id: str, importance: float) -> list[str]:
        """Decide which categories an event should feed."""
        if importance > 0.7:
            return self._config.enabled_categories
        elif importance > 0.4:
            return ["preference_summary", "goal_tracking"]
        else:
            return ["preference_summary"]

    def _generate_reflection(
        self, category: str, candidate: ReflectionCandidate
    ) -> Reflection | None:
        """Generate a reflection from candidate data.

        In a full implementation, this would use an LLM to synthesize insights
        from the source events. For Phase 2, we create structured reflections
        from the accumulated data.
        """
        event_count = len(candidate.source_event_ids)
        episode_count = len(candidate.source_episode_ids)

        # Build reflection content based on category
        templates = {
            "relationship_insight": (
                f"从最近{event_count}个事件和{episode_count}个经历中发现的关系模式"
            ),
            "behavior_pattern": f"用户行为模式分析（基于{event_count}个事件）",
            "goal_tracking": f"用户目标跟踪（{event_count}个相关事件）",
            "preference_summary": f"用户偏好总结更新（{event_count}个偏好事件）",
        }

        content = templates.get(category, f"反思（{category}）: {event_count}个事件")
        plan = None

        # For goal_tracking, suggest a candidate plan
        if category == "goal_tracking" and candidate.accumulated_importance > 0.8:
            plan = "[候选计划] 询问用户关于目标的进展"

        return Reflection(
            reflection_id=f"refl_{generate_ulid()}",
            content=content,
            category=category,
            source_event_ids=list(candidate.source_event_ids),
            source_episode_ids=list(candidate.source_episode_ids),
            confidence=min(0.9, 0.3 + candidate.accumulated_importance * 0.5),
            generated_plan=plan,
            created_at=datetime.now(UTC),
        )
