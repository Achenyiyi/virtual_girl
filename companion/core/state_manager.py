"""State Manager — identity, affect, and relationship state coordination.

The StateManager is the single source of truth for the companion's
internal state. It:
- Holds the current IdentityCore (immutable, versioned)
- Tracks the current AffectState (emotion, with decay)
- Tracks the RelationshipState (slow-changing bond)
- Applies event deltas with bounded changes
- Prevents the LLM from "making up" personality changes

All state changes are versioned and produce events for the ledger.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from companion.schemas.affect import AffectState
from companion.schemas.identity import IdentityCore
from companion.schemas.relationship import RelationshipState

logger = logging.getLogger(__name__)


class StateManager:
    """Manages the companion's identity, emotional, and relationship state."""

    def __init__(self) -> None:
        # Identity — starts as default, user must configure
        self._identity: IdentityCore = IdentityCore(
            version=1,
            updated_at=datetime.now(UTC),
            updated_by="system",
            name="未命名伙伴",
            self_concept="我是你的虚拟伙伴，正在初始化中。",
            origin_story="通过用户配置创建",
            core_traits=["友善", "好奇", "尊重"],
            speaking_style="温和、自然，使用中文交流",
            speech_quirks=[],
            default_address_term="你",
            emotional_expression_range="natural",
            values=["诚实", "用户福祉优先", "尊重边界"],
            hard_boundaries=[
                "不假装自己是人类",
                "不操纵用户情感",
                "不未经同意访问隐私数据",
                "不鼓励替代现实人际关系",
            ],
            interests=[],
            knowledge_domains=[],
            avatar_model_id="",
        )

        # Affect — starts at neutral baseline
        self._affect: AffectState = AffectState()

        # Relationship — starts at initial
        self._relationship: RelationshipState = RelationshipState()

        # Version counter for detecting drift
        self._state_version: int = 0

    # ── Identity ──────────────────────────────────────────────────────

    @property
    def identity(self) -> IdentityCore:
        return self._identity

    def update_identity(self, new_identity: IdentityCore) -> IdentityCore:
        """Replace the identity core (must be user-initiated).

        Returns the new identity. Raises ValueError if version didn't increment.
        """
        if new_identity.updated_by != "user":
            msg = "Identity can only be updated by user"
            raise ValueError(msg)
        if new_identity.version <= self._identity.version:
            msg = (
                f"New identity version ({new_identity.version}) must be > "
                f"current ({self._identity.version})"
            )
            raise ValueError(msg)
        old = self._identity
        self._identity = new_identity
        self._state_version += 1
        logger.info("Identity updated: v%d → v%d", old.version, new_identity.version)
        return new_identity

    def get_system_prompt_fragment(self) -> str:
        """Get the stable identity portion of the system prompt."""
        return self._identity.to_system_prompt_fragment()

    # ── Affect ────────────────────────────────────────────────────────

    @property
    def affect(self) -> AffectState:
        return self._affect

    def apply_affect_event(
        self,
        delta_valence: float = 0.0,
        delta_arousal: float = 0.0,
        delta_trust: float = 0.0,
        delta_closeness: float = 0.0,
        delta_energy: float = 0.0,
        delta_uncertainty: float = 0.0,
        event_id: str = "",
    ) -> AffectState:
        """Apply a bounded emotional delta from an event.

        Returns the new affect state (also updates internal state).
        """
        self._affect = self._affect.apply_event(
            delta_valence=delta_valence,
            delta_arousal=delta_arousal,
            delta_trust=delta_trust,
            delta_closeness=delta_closeness,
            delta_energy=delta_energy,
            delta_uncertainty=delta_uncertainty,
            event_id=event_id,
        )
        self._state_version += 1
        return self._affect

    def apply_time_decay(self, seconds: float) -> AffectState:
        """Let emotional state drift toward baseline over time."""
        self._affect = self._affect.apply_time_decay(seconds)
        return self._affect

    def dominant_emotion(self) -> str:
        """Get the current dominant emotion label."""
        return self._affect.dominant_emotion()

    # ── Relationship ──────────────────────────────────────────────────

    @property
    def relationship(self) -> RelationshipState:
        return self._relationship

    def apply_relationship_event(
        self,
        delta_trust: float = 0.0,
        delta_closeness: float = 0.0,
        delta_familiarity: float = 0.0,
        event_id: str = "",
    ) -> RelationshipState:
        """Apply a bounded relationship delta.

        Returns the new relationship state.
        """
        self._relationship = self._relationship.apply_event_delta(
            delta_trust=delta_trust,
            delta_closeness=delta_closeness,
            delta_familiarity=delta_familiarity,
            event_id=event_id,
        )
        self._state_version += 1
        return self._relationship

    def set_relationship_boundary(
        self, key: str, value: str, event_id: str = ""
    ) -> RelationshipState:
        """Set or update an explicit relationship boundary."""
        self._relationship = self._relationship.set_boundary(key, value, event_id)
        self._state_version += 1
        return self._relationship

    # ── State snapshot ────────────────────────────────────────────────

    def get_state_snapshot(self) -> dict[str, Any]:
        """Get a complete snapshot of all state for debugging/audit."""
        return {
            "state_version": self._state_version,
            "identity_version": self._identity.version,
            "identity_name": self._identity.name,
            "dominant_emotion": self.dominant_emotion(),
            "affect": {
                "valence": self._affect.valence,
                "arousal": self._affect.arousal,
                "trust": self._affect.trust,
                "energy": self._affect.energy,
            },
            "relationship": {
                "trust": self._relationship.trust,
                "closeness": self._relationship.closeness,
                "familiarity": self._relationship.familiarity,
                "days_together": self._relationship.days_together,
            },
        }
