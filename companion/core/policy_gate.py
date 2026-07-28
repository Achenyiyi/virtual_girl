"""Policy Gate — the safety/permission boundary for actions and proactive behavior.

The PolicyGate is the ONLY component that can approve:
1. Proactive behaviors (when the companion initiates interaction)
2. Computer actions (when the companion wants to do something)
3. External communication (sending messages, emails, etc.)

Key principle from the PLAN:
"LLM 只负责提出候选内容，不得自行决定无限观察、打扰或操作电脑"

The policy gate uses a deterministic utility function, not LLM judgment:
relevance + urgency + relationship_value - interruption_cost
- recent_proactives - user_rejection_signal
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class ProactiveLevel(IntEnum):
    """Five-level proactive behavior hierarchy (from PLAN Section 6.4)."""

    LEVEL_0_IDLE = 0  # Breathing, blinking, idle posture
    LEVEL_1_SUBTLE = 1  # Facial expression, silent bubble
    LEVEL_2_HINT = 2  # Short text hint, one-click dismiss
    LEVEL_3_CONVERSATION = 3  # Active speech, high-value low-interruption
    LEVEL_4_ACTION = 4  # Propose computer action, must respect permissions


@dataclass
class ProactiveDecision:
    """The result of evaluating whether the companion should initiate."""

    allowed: bool = False
    level: ProactiveLevel = ProactiveLevel.LEVEL_0_IDLE
    reason: str = ""
    utility_score: float = 0.0

    # Factors
    relevance: float = 0.0  # How relevant is this to current context?
    urgency: float = 0.0  # How time-sensitive?
    relationship_value: float = 0.0  # How much does this strengthen the bond?
    interruption_cost: float = 0.0  # How disruptive would this be?
    recent_proactive_count: int = 0  # How many proactives in the last hour?
    user_rejection_score: float = 0.0  # Recent rejection rate


@dataclass
class ActionDecision:
    """The result of evaluating a computer action request."""

    approved: bool = False
    requires_user_confirmation: bool = True
    preview_text: str = ""
    risk_level: str = "irreversible"
    reason: str = ""
    cooldown_until: float = 0.0  # Unix timestamp


@dataclass(frozen=True)
class PolicyGateConfig:
    """Deterministic proactive-policy limits loaded from runtime configuration."""

    quiet_hours_enabled: bool = True
    quiet_hours_start_hour: int = 23
    quiet_hours_end_hour: int = 7
    level_1_per_hour: int = 30
    level_2_per_hour: int = 10
    level_3_per_hour: int = 3
    level_4_per_hour: int = 1
    level_1_cooldown_seconds: float = 5.0
    level_2_cooldown_seconds: float = 30.0
    level_3_cooldown_seconds: float = 300.0
    level_4_cooldown_seconds: float = 1800.0

    def __post_init__(self) -> None:
        if not 0 <= self.quiet_hours_start_hour <= 23:
            raise ValueError("quiet_hours_start_hour must be between 0 and 23")
        if not 0 <= self.quiet_hours_end_hour <= 23:
            raise ValueError("quiet_hours_end_hour must be between 0 and 23")
        budgets = (
            self.level_1_per_hour,
            self.level_2_per_hour,
            self.level_3_per_hour,
            self.level_4_per_hour,
        )
        if any(value < 0 for value in budgets):
            raise ValueError("proactive hourly budgets must not be negative")
        cooldowns = (
            self.level_1_cooldown_seconds,
            self.level_2_cooldown_seconds,
            self.level_3_cooldown_seconds,
            self.level_4_cooldown_seconds,
        )
        if any(value < 0 for value in cooldowns):
            raise ValueError("proactive cooldowns must not be negative")


class PolicyGate:
    """Deterministic policy engine for proactive behavior and action approval.

    Policies are rules, not suggestions. The PolicyGate does NOT use
    LLM judgment — it uses configurable thresholds and counts.
    """

    def __init__(self, config: PolicyGateConfig | None = None) -> None:
        config = config or PolicyGateConfig()
        # Proactive budget
        self._max_proactives_per_hour: dict[ProactiveLevel, int] = {
            ProactiveLevel.LEVEL_1_SUBTLE: config.level_1_per_hour,
            ProactiveLevel.LEVEL_2_HINT: config.level_2_per_hour,
            ProactiveLevel.LEVEL_3_CONVERSATION: config.level_3_per_hour,
            ProactiveLevel.LEVEL_4_ACTION: config.level_4_per_hour,
        }
        self._proactive_history: deque[tuple[float, ProactiveLevel]] = deque()

        # Quiet hours
        self._quiet_hours_start_hour = config.quiet_hours_start_hour
        self._quiet_hours_end_hour = config.quiet_hours_end_hour
        self._quiet_hours_enabled = config.quiet_hours_enabled

        # Cooldown between proactives (seconds)
        self._cooldown: dict[ProactiveLevel, float] = {
            ProactiveLevel.LEVEL_1_SUBTLE: config.level_1_cooldown_seconds,
            ProactiveLevel.LEVEL_2_HINT: config.level_2_cooldown_seconds,
            ProactiveLevel.LEVEL_3_CONVERSATION: config.level_3_cooldown_seconds,
            ProactiveLevel.LEVEL_4_ACTION: config.level_4_cooldown_seconds,
        }

        # Action permissions
        self._action_cooldowns: dict[str, float] = {}

        # User feedback tracking
        self._recent_acceptances: int = 0
        self._recent_rejections: int = 0
        self._recent_total: int = 0
        self._max_recent_feedback: int = 20

        # State flags
        self._user_in_meeting: bool = False
        self._user_in_fullscreen: bool = False
        self._user_typing_intensely: bool = False
        self._muted: bool = False  # Manual mute by user

    # ── Proactive decision ────────────────────────────────────────────

    def evaluate_proactive(
        self,
        proposed_level: ProactiveLevel,
        relevance: float = 0.5,
        urgency: float = 0.0,
        relationship_value: float = 0.3,
    ) -> ProactiveDecision:
        """Evaluate whether a proactive behavior should be allowed.

        Returns a decision with the approved level (may downgrade).
        """
        now = time.time()

        # Manual mute: only Level 0 (idle animations)
        if self._muted:
            return ProactiveDecision(
                allowed=False,
                level=ProactiveLevel.LEVEL_0_IDLE,
                reason="User muted",
            )

        # Quiet hours: suppress Level 2+
        if (
            self._quiet_hours_enabled
            and self._is_quiet_hours()
            and proposed_level >= ProactiveLevel.LEVEL_2_HINT
        ):
            return ProactiveDecision(
                allowed=False,
                level=ProactiveLevel.LEVEL_0_IDLE,
                reason="Quiet hours",
            )

        # Active context checks
        if self._user_in_meeting and proposed_level >= ProactiveLevel.LEVEL_2_HINT:
            return ProactiveDecision(
                allowed=False,
                level=ProactiveLevel.LEVEL_0_IDLE,
                reason="User in meeting",
            )
        if self._user_in_fullscreen and proposed_level >= ProactiveLevel.LEVEL_3_CONVERSATION:
            return ProactiveDecision(
                allowed=False,
                level=ProactiveLevel.LEVEL_0_IDLE,
                reason="User in fullscreen",
            )
        if self._user_typing_intensely and proposed_level >= ProactiveLevel.LEVEL_3_CONVERSATION:
            return ProactiveDecision(
                allowed=False,
                level=ProactiveLevel.LEVEL_0_IDLE,
                reason="User typing intensely",
            )

        # Level 0 is always allowed (idle animations)
        if proposed_level == ProactiveLevel.LEVEL_0_IDLE:
            return ProactiveDecision(
                allowed=True,
                level=ProactiveLevel.LEVEL_0_IDLE,
                reason="Level 0 always allowed",
            )

        # Budget check
        hourly_count = self._count_recent_proactives(proposed_level, 3600)
        max_allowed = self._max_proactives_per_hour.get(proposed_level, 0)
        if hourly_count >= max_allowed:
            if proposed_level == ProactiveLevel.LEVEL_1_SUBTLE:
                return ProactiveDecision(
                    allowed=False,
                    level=ProactiveLevel.LEVEL_0_IDLE,
                    reason="Hourly budget exhausted for Level 1",
                    recent_proactive_count=hourly_count,
                )
            downgraded = ProactiveLevel(int(proposed_level) - 1)
            return self.evaluate_proactive(downgraded, relevance, urgency, relationship_value)

        # Cooldown check
        last_time = self._last_proactive_time(proposed_level)
        cooldown = self._cooldown.get(proposed_level, 60.0)
        if last_time and (now - last_time) < cooldown:
            return ProactiveDecision(
                allowed=False,
                level=ProactiveLevel.LEVEL_0_IDLE,
                reason=f"Cooldown: {cooldown - (now - last_time):.0f}s remaining",
            )

        # Utility function
        interruption_cost = self._compute_interruption_cost()
        rejection_score = self._compute_rejection_score()
        utility = (
            relevance
            + urgency
            + relationship_value
            - interruption_cost
            - 0.1 * hourly_count
            - rejection_score
        )

        # Threshold for approval
        threshold = {
            ProactiveLevel.LEVEL_1_SUBTLE: 0.0,
            ProactiveLevel.LEVEL_2_HINT: 0.3,
            ProactiveLevel.LEVEL_3_CONVERSATION: 0.6,
            ProactiveLevel.LEVEL_4_ACTION: 0.8,
        }.get(proposed_level, 1.0)

        allowed = utility >= threshold

        if allowed:
            self._proactive_history.append((now, proposed_level))

        return ProactiveDecision(
            allowed=allowed,
            level=proposed_level if allowed else ProactiveLevel.LEVEL_0_IDLE,
            reason=f"Utility {utility:.2f} {'>=' if allowed else '<'} threshold {threshold}",
            utility_score=utility,
            relevance=relevance,
            urgency=urgency,
            relationship_value=relationship_value,
            interruption_cost=interruption_cost,
            recent_proactive_count=hourly_count,
            user_rejection_score=rejection_score,
        )

    # ── Action decision ───────────────────────────────────────────────

    def evaluate_action(
        self,
        action_type: str,
        parameters: dict[str, Any] | None = None,
    ) -> ActionDecision:
        """Evaluate whether a computer action should be allowed.

        The risk level is derived from the action classification table,
        not caller-provided. This ensures consistent enforcement.

        Returns a decision with approval status and any requirements.
        """
        from companion.schemas.action_classification import get_action_classification

        classification = get_action_classification(action_type)
        actual_risk, _, requires_conf, can_undo = classification

        # Irreversible actions always need confirmation
        if actual_risk == "irreversible":
            return ActionDecision(
                approved=True,  # Approved PENDING confirmation
                requires_user_confirmation=True,
                preview_text=f"About to {action_type} with {parameters}",
                risk_level=actual_risk,
                reason="Irreversible action requires explicit user confirmation",
            )

        # Check action cooldown
        now = time.time()
        cooldown_key = f"action:{action_type}"
        cooldown_until = self._action_cooldowns.get(cooldown_key, 0.0)
        if now < cooldown_until:
            return ActionDecision(
                approved=False,
                risk_level=actual_risk,
                reason=f"Action cooldown active until {cooldown_until - now:.0f}s",
                cooldown_until=cooldown_until,
            )

        # Read-only: auto-approve
        if actual_risk == "readonly":
            return ActionDecision(
                approved=True,
                requires_user_confirmation=False,
                risk_level=actual_risk,
                reason="Read-only operation: auto-approved",
            )

        # Reversible low: auto-approve per policy
        if actual_risk == "reversible_low":
            return ActionDecision(
                approved=True,
                requires_user_confirmation=False,
                risk_level=actual_risk,
                reason="Reversible low-risk: auto-approved",
            )

        # Reversible high: approve with preview
        return ActionDecision(
            approved=True,
            requires_user_confirmation=requires_conf,
            preview_text=f"About to {action_type} with {parameters}",
            risk_level=actual_risk,
            reason="Reversible high-risk: preview required",
        )

    # ── Feedback ──────────────────────────────────────────────────────

    def record_feedback(self, accepted: bool) -> None:
        """Record user feedback on a proactive behavior.

        Uses a circular-buffer approximation when the feedback window overflows,
        preserving the accept/reject ratio as closely as possible.
        """
        if accepted:
            self._recent_acceptances += 1
        else:
            self._recent_rejections += 1
        self._recent_total += 1

        # Trim to window using proportional reduction.
        # This is an approximation — for exact tracking, a deque-based
        # sliding window should replace the simple counter approach.
        excess = self._recent_total - self._max_recent_feedback
        if excess > 0:
            total = max(1, self._recent_total)
            ratio_reject = self._recent_rejections / total
            self._recent_rejections = max(0, self._recent_rejections - int(excess * ratio_reject))
            self._recent_acceptances = max(
                0, self._recent_acceptances - int(excess * (1 - ratio_reject))
            )
            self._recent_total = self._recent_acceptances + self._recent_rejections

    # ── Context setters ───────────────────────────────────────────────

    def set_user_context(
        self,
        in_meeting: bool | None = None,
        in_fullscreen: bool | None = None,
        typing_intensely: bool | None = None,
    ) -> None:
        """Update the user's current context for policy evaluation."""
        if in_meeting is not None:
            self._user_in_meeting = in_meeting
        if in_fullscreen is not None:
            self._user_in_fullscreen = in_fullscreen
        if typing_intensely is not None:
            self._user_typing_intensely = typing_intensely

    def set_muted(self, muted: bool) -> None:
        self._muted = muted

    @property
    def is_muted(self) -> bool:
        return self._muted

    # ── Internals ─────────────────────────────────────────────────────

    def _is_quiet_hours(self) -> bool:
        """Check if current local time is within quiet hours."""
        import datetime

        now = datetime.datetime.now()
        hour = now.hour
        if self._quiet_hours_start_hour < self._quiet_hours_end_hour:
            return self._quiet_hours_start_hour <= hour < self._quiet_hours_end_hour
        else:
            # Wraps around midnight (e.g., 23:00 to 07:00)
            return hour >= self._quiet_hours_start_hour or hour < self._quiet_hours_end_hour

    def _count_recent_proactives(self, level: ProactiveLevel, window_seconds: float) -> int:
        """Count proactives of a given level within the time window."""
        now = time.time()
        cutoff = now - window_seconds
        while self._proactive_history and self._proactive_history[0][0] < cutoff:
            self._proactive_history.popleft()
        return sum(1 for ts, lvl in self._proactive_history if lvl == level and ts >= cutoff)

    def _last_proactive_time(self, level: ProactiveLevel) -> float | None:
        """Get the timestamp of the last proactive at this level."""
        for ts, lvl in reversed(self._proactive_history):
            if lvl == level:
                return ts
        return None

    def _compute_interruption_cost(self) -> float:
        """Estimate how disruptive a proactive would be right now.

        Returns 0.0 (no disruption) to 1.0 (maximum disruption).
        """
        cost = 0.0
        if self._user_in_meeting:
            cost += 0.8
        if self._user_in_fullscreen:
            cost += 0.5
        if self._user_typing_intensely:
            cost += 0.3
        return min(1.0, cost)

    def _compute_rejection_score(self) -> float:
        """Compute recent user rejection rate as a penalty.

        Returns 0.0 (no recent rejections) to 1.0 (all recent were rejected).
        """
        if self._recent_total == 0:
            return 0.0
        return self._recent_rejections / self._recent_total
