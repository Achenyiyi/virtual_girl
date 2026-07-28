"""Emotion and affect state events.

The emotional model uses continuous low-dimensional state
(valence, arousal, trust, closeness, energy, uncertainty)
rather than discrete emotion labels.

State changes are bounded — no single event can cause
a full-range jump. State drifts toward baseline over time.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from companion.events.base import BaseEvent


class AffectStateUpdatedEvent(BaseEvent):
    """The companion's continuous affect state changed.

    Each field has a bounded delta per event. The state
    naturally decays toward baseline between events.
    """

    __event_type__: ClassVar[str] = "emotion.affect.updated"

    # New state values (after update applied)
    valence: float = Field(..., ge=-1, le=1, description="Pleasantness (-1 to +1)")
    arousal: float = Field(..., ge=0, le=1, description="Activation level (0 to 1)")
    trust: float = Field(..., ge=0, le=1, description="Trust in user (0 to 1)")
    closeness: float = Field(..., ge=0, le=1, description="Emotional closeness (0 to 1)")
    energy: float = Field(..., ge=0, le=1, description="Current energy level (0 to 1)")
    uncertainty: float = Field(..., ge=0, le=1, description="Situational uncertainty (0 to 1)")

    # Deltas applied (for audit)
    delta_valence: float = Field(default=0, ge=-0.3, le=0.3)
    delta_arousal: float = Field(default=0, ge=-0.2, le=0.2)
    delta_trust: float = Field(default=0, ge=-0.1, le=0.1)
    delta_closeness: float = Field(default=0, ge=-0.05, le=0.05)
    delta_energy: float = Field(default=0, ge=-0.3, le=0.3)
    delta_uncertainty: float = Field(default=0, ge=-0.3, le=0.3)

    trigger_event_id: str = Field(
        default="",
        description="The event that caused this state change",
    )
    trigger_type: str = Field(
        default="conversation",
        description="Category: 'conversation', 'perception', 'reflection', 'time_decay'",
    )


class RelationshipStateUpdatedEvent(BaseEvent):
    """A relationship dimension changed due to accumulated evidence.

    Relationship state changes slowly — it requires multiple
    consistent events over time. Single-event jumps are not allowed
    beyond the bounded delta.
    """

    __event_type__: ClassVar[str] = "emotion.relationship.updated"

    trust: float | None = Field(default=None, ge=0, le=1)
    closeness: float | None = Field(default=None, ge=0, le=1)
    nickname: str | None = Field(default=None, description="Current nickname/address term")
    shared_references: list[str] = Field(
        default_factory=list,
        description="Inside jokes, shared memories, callbacks",
    )
    boundaries: dict[str, str] = Field(
        default_factory=dict,
        description="Explicit boundaries set by user (topic, tone, time)",
    )
    evidence_event_ids: list[str] = Field(
        ...,
        min_length=1,
        description="Events that triggered this relationship update",
    )


class EmotionalExpressionEvent(BaseEvent):
    """The companion expressed an emotion through voice/facial expression/gesture.

    This records the mapping from internal state to external expression.
    """

    __event_type__: ClassVar[str] = "emotion.expression"

    turn_id: str | None = Field(default=None, description="Associated conversation turn")
    expression_type: str = Field(
        ...,
        description="e.g., 'facial', 'gesture', 'voicetone', 'posture'",
    )
    expression_id: str = Field(..., description="Asset/parameter identifier")
    intensity: float = Field(default=0.5, ge=0, le=1)
    source_valence: float = Field(default=0, ge=-1, le=1)
    source_arousal: float = Field(default=0.5, ge=0, le=1)
