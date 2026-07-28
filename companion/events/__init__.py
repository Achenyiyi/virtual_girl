"""Domain event schemas — the immutable event ledger.

All significant state changes are recorded as typed domain events.
Events are append-only and serve as the single source of truth for
memory, relationships, and personality.
"""

from companion.events.base import (
    BaseEvent,
    EventHeader,
    EventPrivacy,
    EventSource,
)
from companion.events.registry import EventRegistry, get_registry

__all__ = [
    "BaseEvent",
    "EventHeader",
    "EventPrivacy",
    "EventRegistry",
    "EventSource",
    "get_registry",
]
