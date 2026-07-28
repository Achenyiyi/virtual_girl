"""Event type registry for validation and discovery.

All event types must be registered before use. The registry
enforces schema versioning and enables event reconstruction.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from companion.events.base import BaseEvent


class EventRegistry:
    """Central registry of all known event types.

    Enforces:
    - Every event type has a unique fully-qualified name
    - Event types can be looked up for deserialization
    - Schema versions are tracked for migration
    """

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._events: dict[str, type[BaseEvent]] = {}
        self._versions: dict[str, int] = {}
        self._initialized: bool = False

    def register(self, event_cls: type[BaseEvent]) -> type[BaseEvent]:
        """Register an event class. Thread-safe. Returns the class for decorator use."""
        event_type = event_cls.__event_type__
        with self._lock:
            if event_type in self._events:
                existing = self._events[event_type]
                if existing is not event_cls:
                    msg = f"Event type '{event_type}' already registered by {existing.__name__}"
                    raise ValueError(msg)

            self._events[event_type] = event_cls
            self._versions[event_type] = 1
        return event_cls

    def get(self, event_type: str) -> type[BaseEvent] | None:
        """Look up an event class by its fully qualified type string."""
        with self._lock:
            return self._events.get(event_type)

    def list_types(self) -> list[str]:
        """Return all registered event type strings, sorted."""
        with self._lock:
            return sorted(self._events.keys())

    def is_registered(self, event_type: str) -> bool:
        """Check if an event type string is known."""
        with self._lock:
            return event_type in self._events

    def deserialize(self, event_type: str, data: dict[str, Any]) -> BaseEvent:
        """Deserialize an event from a dict, validating against schema.

        Raises:
            KeyError: if event_type is not registered
            ValidationError: if data doesn't match the schema
        """
        with self._lock:
            cls = self._events.get(event_type)
        if cls is None:
            with self._lock:
                valid = ", ".join(sorted(self._events.keys())[:10])
            msg = f"Unknown event type '{event_type}'. Valid types include: {valid}..."
            raise KeyError(msg)
        return cls.model_validate(data)

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

    def __contains__(self, event_type: str) -> bool:
        with self._lock:
            return event_type in self._events


# Global singleton
_registry: EventRegistry | None = None


def get_registry() -> EventRegistry:
    """Get or create the global event registry singleton."""
    global _registry
    if _registry is None:
        _registry = EventRegistry()
        _register_all(_registry)
    return _registry


def _register_all(registry: EventRegistry) -> None:
    """Register all built-in event types."""
    from companion.events.action import (  # noqa: F401
        ActionConfirmedEvent,
        ActionExecutedEvent,
        ActionRequestedEvent,
        ActionRevokedEvent,
        ToolAuditEvent,
    )
    from companion.events.conversation import (  # noqa: F401
        AsrFinalizedEvent,
        AudioPlayedEvent,
        ConversationTurnCompletedEvent,
        ConversationTurnFailedEvent,
        ConversationTurnInterruptedEvent,
        ConversationTurnStartedEvent,
        LlmResponseGeneratedEvent,
        TtsSynthesizedEvent,
    )
    from companion.events.emotion import (  # noqa: F401
        AffectStateUpdatedEvent,
        EmotionalExpressionEvent,
        RelationshipStateUpdatedEvent,
    )
    from companion.events.lifecycle import (  # noqa: F401
        CompanionShutdownEvent,
        CompanionStartupEvent,
        SessionEndedEvent,
        SessionStartedEvent,
    )
    from companion.events.memory import (  # noqa: F401
        EpisodeCreatedEvent,
        FactExtractedEvent,
        FactUpdatedEvent,
        MemoryForgottenEvent,
        MemoryRebuiltEvent,
        ReflectionCreatedEvent,
    )
    from companion.events.perception import (  # noqa: F401
        AppFocusChangedEvent,
        MediaPlaybackEvent,
        ScheduleEventDetectedEvent,
        UserActivityStateChangedEvent,
    )
    from companion.events.shared_experience import (  # noqa: F401
        MilestoneReachedEvent,
        PlanCreatedEvent,
        PlanExecutedEvent,
        SharedExperienceCompletedEvent,
    )

    for cls in (
        # Conversation
        ConversationTurnStartedEvent,
        AsrFinalizedEvent,
        LlmResponseGeneratedEvent,
        TtsSynthesizedEvent,
        AudioPlayedEvent,
        ConversationTurnInterruptedEvent,
        ConversationTurnFailedEvent,
        ConversationTurnCompletedEvent,
        # Memory
        FactExtractedEvent,
        FactUpdatedEvent,
        EpisodeCreatedEvent,
        ReflectionCreatedEvent,
        MemoryForgottenEvent,
        MemoryRebuiltEvent,
        # Emotion
        AffectStateUpdatedEvent,
        RelationshipStateUpdatedEvent,
        EmotionalExpressionEvent,
        # Perception
        AppFocusChangedEvent,
        UserActivityStateChangedEvent,
        MediaPlaybackEvent,
        ScheduleEventDetectedEvent,
        # Action
        ActionRequestedEvent,
        ActionConfirmedEvent,
        ActionExecutedEvent,
        ActionRevokedEvent,
        ToolAuditEvent,
        # Lifecycle
        CompanionStartupEvent,
        CompanionShutdownEvent,
        SessionStartedEvent,
        SessionEndedEvent,
        # Shared Experience
        SharedExperienceCompletedEvent,
        MilestoneReachedEvent,
        PlanCreatedEvent,
        PlanExecutedEvent,
    ):
        registry.register(cls)
