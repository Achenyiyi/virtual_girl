"""Memory events — the five-layer memory system lifecycle.

Layer 1: event_log    — raw immutable events (these events *about* memory)
Layer 2: working_memory — current session context
Layer 3: semantic_facts  — user preferences, identity, stable facts
Layer 4: episodic_memory — shared experiences with temporal/causal links
Layer 5: reflections     — synthesized insights from multiple events

All derived memory (facts, episodes, reflections) must reference
their source events for audit, correction cascades, and rebuild.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from pydantic import Field

from companion.events.base import BaseEvent


class FactExtractedEvent(BaseEvent):
    """A semantic fact was extracted from raw events."""

    __event_type__: ClassVar[str] = "memory.fact.extracted"

    fact_id: str = Field(..., description="Unique fact identifier")
    key: str = Field(..., description="Fact key (normalized)")
    value: str = Field(..., description="Fact value")
    category: str = Field(
        default="general",
        description="e.g., 'preference', 'identity', 'relationship', 'schedule'",
    )
    confidence: float = Field(..., ge=0, le=1)
    valid_from: datetime = Field(..., description="When this fact became true")
    valid_to: datetime | None = Field(
        default=None, description="When it stopped being true (None = current)"
    )
    source_event_ids: list[str] = Field(
        ...,
        min_length=1,
        description="Event IDs this fact was derived from",
    )
    extraction_method: str = Field(
        default="llm",
        description="How this fact was extracted: 'llm', 'structured', 'user_explicit'",
    )


class FactUpdatedEvent(BaseEvent):
    """A fact was updated — old version closed, new version created.

    We never overwrite facts. Updates close the old fact's validity
    and create a new fact entry. This preserves history and enables
    rollback and audit.
    """

    __event_type__: ClassVar[str] = "memory.fact.updated"

    old_fact_id: str = Field(..., description="Fact being superseded")
    new_fact_id: str = Field(..., description="New fact replacing it")
    key: str = Field(..., description="Fact key that changed")
    old_value: str = Field(default="")
    new_value: str = Field(default="")
    reason: str = Field(
        default="user_correction",
        description="Why: 'user_correction', 'new_evidence', 'contradiction'",
    )


class EpisodeCreatedEvent(BaseEvent):
    """An episodic memory was formed from completed conversation turns."""

    __event_type__: ClassVar[str] = "memory.episode.created"

    episode_id: str = Field(..., description="Unique episode identifier")
    title: str = Field(..., description="Short summary of the episode")
    summary: str = Field(..., description="Narrative summary")
    participants: list[str] = Field(default_factory=lambda: ["user", "companion"])
    emotional_salience: float = Field(default=0.5, ge=0, le=1)
    turn_ids: list[str] = Field(..., description="Conversation turns in this episode")
    tags: list[str] = Field(default_factory=list)
    episode_occurred_at: datetime = Field(..., description="When the episode actually happened")


class ReflectionCreatedEvent(BaseEvent):
    """A reflection was synthesized from multiple events/episodes."""

    __event_type__: ClassVar[str] = "memory.reflection.created"

    reflection_id: str = Field(..., description="Unique reflection identifier")
    content: str = Field(..., description="Reflection text")
    category: str = Field(
        default="general",
        description="e.g., 'relationship_insight', 'goal_tracking', 'behavior_pattern'",
    )
    source_event_ids: list[str] = Field(..., min_length=1)
    source_episode_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)
    generated_plan: str | None = Field(
        default=None,
        description="If this reflection produced a candidate plan for action",
    )


class MemoryForgottenEvent(BaseEvent):
    """User requested to forget specific memories. Triggers cascade deletion."""

    __event_type__: ClassVar[str] = "memory.forgotten"

    event_ids_to_delete: list[str] = Field(
        ...,
        min_length=1,
        description="Source events to delete (cascaded to all derived memory)",
    )
    reason: str = Field(default="user_request")
    cascade_count: int = Field(
        default=0,
        description="Number of derived memory items affected by cascade",
    )


class MemoryRebuiltEvent(BaseEvent):
    """All derived memory was rebuilt from the event log (verification/recovery)."""

    __event_type__: ClassVar[str] = "memory.rebuilt"

    event_count: int = Field(..., description="Number of source events replayed")
    facts_restored: int = Field(default=0)
    episodes_restored: int = Field(default=0)
    reflections_restored: int = Field(default=0)
    consistency_errors: int = Field(default=0)
    duration_ms: int = Field(default=0)
    passed_consistency_check: bool = Field(default=False)
