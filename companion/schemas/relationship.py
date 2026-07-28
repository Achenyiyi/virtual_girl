"""Relationship State — slowly-changing dimensions of the user-companion bond.

Relationship state changes ONLY through accumulated, evidence-backed
events. A single conversation cannot dramatically shift trust or closeness.
The model cannot "decide" to change the relationship; events in the ledger
produce gradual deltas with bounded magnitudes.

Key principle from the PLAN:
"关系状态只能被可解释事件长期改变，不允许模型一句话把亲密度从陌生跳到恋人"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class RelationshipState:
    """Current snapshot of the relationship between user and companion."""

    # Core dimensions (0 to 1)
    trust: float = 0.3  # Trust starts low, grows with consistency
    closeness: float = 0.1  # Closeness starts very low
    familiarity: float = 0.0  # How well the companion "knows" the user

    # Address & communication
    nickname: str = ""  # Current nickname for the user
    preferred_address_term: str = ""  # How the companion currently addresses user
    formality_level: float = 0.5  # 0 = casual, 1 = formal

    # Shared context
    shared_references: tuple[str, ...] = ()  # Inside jokes, callbacks
    shared_experience_count: int = 0
    days_together: int = 0

    def add_shared_reference(self, ref: str) -> RelationshipState:
        """Append a shared reference, returning a new state."""
        return RelationshipState(
            trust=self.trust,
            closeness=self.closeness,
            familiarity=self.familiarity,
            nickname=self.nickname,
            preferred_address_term=self.preferred_address_term,
            formality_level=self.formality_level,
            shared_references=self.shared_references + (ref,),
            shared_experience_count=self.shared_experience_count,
            days_together=self.days_together,
            boundaries=dict(self.boundaries),
            milestones=self.milestones,
            last_updated_at=self.last_updated_at,
            last_updated_event_id=self.last_updated_event_id,
            version=self.version,
        )

    def add_milestone(self, milestone: str) -> RelationshipState:
        """Append a milestone, returning a new state."""
        return RelationshipState(
            trust=self.trust,
            closeness=self.closeness,
            familiarity=self.familiarity,
            nickname=self.nickname,
            preferred_address_term=self.preferred_address_term,
            formality_level=self.formality_level,
            shared_references=self.shared_references,
            shared_experience_count=self.shared_experience_count,
            days_together=self.days_together,
            boundaries=dict(self.boundaries),
            milestones=self.milestones + (milestone,),
            last_updated_at=self.last_updated_at,
            last_updated_event_id=self.last_updated_event_id,
            version=self.version,
        )

    # Explicit boundaries set by user
    boundaries: dict[str, str] = field(default_factory=dict)
    # e.g., {"topic_politics": "avoid", "time_late_night": "quiet_mode"}

    # Milestones reached
    milestones: tuple[str, ...] = ()

    # Last significant update
    last_updated_at: datetime | None = None
    last_updated_event_id: str = ""

    # Version for change tracking
    version: int = 0

    def apply_event_delta(
        self,
        delta_trust: float = 0.0,
        delta_closeness: float = 0.0,
        delta_familiarity: float = 0.0,
        event_id: str = "",
    ) -> RelationshipState:
        """Create a new state with bounded deltas applied.

        Deltas are deliberately capped to prevent single-event jumps.
        """
        return RelationshipState(
            trust=max(0.0, min(1.0, self.trust + max(-0.1, min(0.1, delta_trust)))),
            closeness=max(0.0, min(1.0, self.closeness + max(-0.05, min(0.05, delta_closeness)))),
            familiarity=max(
                0.0, min(1.0, self.familiarity + max(-0.1, min(0.1, delta_familiarity)))
            ),
            nickname=self.nickname,
            preferred_address_term=self.preferred_address_term,
            formality_level=self.formality_level,
            shared_references=self.shared_references,
            shared_experience_count=self.shared_experience_count,
            days_together=self.days_together,
            boundaries=dict(self.boundaries),
            milestones=self.milestones,
            last_updated_at=datetime.now(),
            last_updated_event_id=event_id,
            version=self.version + 1,
        )

    def set_boundary(self, key: str, value: str, event_id: str = "") -> RelationshipState:
        """Set an explicit boundary."""
        new_boundaries = dict(self.boundaries)
        new_boundaries[key] = value
        return RelationshipState(
            trust=self.trust,
            closeness=self.closeness,
            familiarity=self.familiarity,
            nickname=self.nickname,
            preferred_address_term=self.preferred_address_term,
            formality_level=self.formality_level,
            shared_references=self.shared_references,
            shared_experience_count=self.shared_experience_count,
            days_together=self.days_together,
            boundaries=new_boundaries,
            milestones=self.milestones,
            last_updated_at=datetime.now(),
            last_updated_event_id=event_id,
            version=self.version + 1,
        )
