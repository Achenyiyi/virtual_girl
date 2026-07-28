"""Structured data schemas for identity, relationship, affect, and actions.

These define the companion's internal state that persists across sessions.
All schemas are immutable value objects — changes produce new instances.
"""

from companion.schemas.action_classification import (
    ActionMethod,
    RiskLevel,
    get_action_classification,
)
from companion.schemas.affect import AffectState
from companion.schemas.identity import IdentityCore
from companion.schemas.relationship import RelationshipState

__all__ = [
    "IdentityCore",
    "RelationshipState",
    "AffectState",
    "ActionMethod",
    "RiskLevel",
    "get_action_classification",
]
