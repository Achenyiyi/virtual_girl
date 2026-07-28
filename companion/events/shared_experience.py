"""Shared experience events — joint activities between user and companion.

These events form the basis of "shared memories" — moments the user
and companion experienced together. They are the raw material for:
- Anniversary/milestone tracking
- Relationship depth growth
- Conversation callbacks ("Remember when we...")
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from pydantic import Field

from companion.events.base import BaseEvent


class SharedExperienceCompletedEvent(BaseEvent):
    """User and companion completed a shared activity together.

    This is one of the most important event types in the system —
    it records the emotional content of shared experiences that
    form the basis of the relationship.
    """

    __event_type__: ClassVar[str] = "shared_experience.completed"

    activity_id: str = Field(..., description="Unique activity identifier")
    activity_type: str = Field(
        ...,
        description=(
            "e.g., 'finished_game_chapter', 'watched_movie', 'cooked_together', 'studied_together'"
        ),
    )
    activity_label: str = Field(..., description="Human-readable activity name")
    duration_minutes: int = Field(default=0, ge=0)
    user_reaction: str = Field(
        default="neutral",
        description="Emotional reaction: 'excited', 'happy', 'neutral', 'frustrated', 'sad'",
    )
    companion_reaction: str = Field(
        default="neutral",
        description="Companion's expressed reaction",
    )
    significance: float = Field(
        default=0.5,
        ge=0,
        le=1,
        description="How significant this experience is (0 = trivial, 1 = life-changing)",
    )
    tags: list[str] = Field(
        default_factory=list, description="e.g., ['gaming', 'achievement', 'weekend']"
    )
    memories_made: list[str] = Field(
        default_factory=list,
        description="Short memory snippets to preserve (max 5)",
    )


class MilestoneReachedEvent(BaseEvent):
    """A relationship or usage milestone was reached."""

    __event_type__: ClassVar[str] = "shared_experience.milestone"

    milestone_id: str = Field(...)
    milestone_type: str = Field(
        ...,
        description="e.g., 'days_together', 'conversation_count', 'first_shared_activity'",
    )
    milestone_value: int | str = Field(...)
    is_first_time: bool = Field(default=True)
    commemorated: bool = Field(
        default=False, description="Whether companion acknowledged this milestone"
    )


class PlanCreatedEvent(BaseEvent):
    """A future plan or goal was created (by user or companion slow-loop)."""

    __event_type__: ClassVar[str] = "shared_experience.plan.created"

    plan_id: str = Field(...)
    plan_title: str = Field(..., description="Short, actionable title")
    plan_detail: str = Field(default="")
    proposed_by: str = Field(default="companion", description="'user' or 'companion'")
    target_date: datetime | None = Field(default=None)
    priority: int = Field(default=3, ge=1, le=5, description="1=highest, 5=lowest")
    status: str = Field(
        default="proposed",
        description="'proposed', 'accepted', 'in_progress', 'completed', 'cancelled'",
    )


class PlanExecutedEvent(BaseEvent):
    """A plan was acted upon."""

    __event_type__: ClassVar[str] = "shared_experience.plan.executed"

    plan_id: str = Field(...)
    outcome: str = Field(
        default="completed", description="'completed', 'partial', 'failed', 'cancelled'"
    )
    user_feedback: str | None = Field(default=None)
    context_changed: bool = Field(
        default=False,
        description="Whether the situation changed from when the plan was made",
    )
