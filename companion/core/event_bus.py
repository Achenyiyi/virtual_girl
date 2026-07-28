"""Local Event Bus — typed, asynchronous event distribution.

The EventBus is the central nervous system of the companion. Components
publish typed domain events and subscribe to event types they care about.

Key properties:
- Events are delivered asynchronously (fire-and-forget by default)
- Subscribers can be synchronous (blocking) or async
- Events carry their schema version for compatibility
- The bus supports event replay for testing and recovery
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable

from companion.events.base import BaseEvent

logger = logging.getLogger(__name__)

# An event handler is a callable that takes a BaseEvent and returns None
EventHandler = Callable[[BaseEvent], Awaitable[None]] | Callable[[BaseEvent], None]
EventPersistenceHandler = Callable[[BaseEvent], Awaitable[object]]


class EventBus:
    """Typed pub/sub event bus for inter-component communication.

    Example:
        bus = EventBus()

        @bus.on("conversation.turn.completed")
        async def on_turn_completed(event):
            print(f"Turn {event.turn_id} completed")

        await bus.publish(turn_completed_event)
    """

    def __init__(
        self,
        name: str = "default",
        max_log_size: int = 10_000,
        persistence_handler: EventPersistenceHandler | None = None,
    ) -> None:
        self.name = name
        self._subscribers: dict[str, list[EventHandler]] = defaultdict(list)
        self._wildcard_subscribers: list[EventHandler] = []
        self._event_log: list[BaseEvent] = []  # For replay/debugging
        self._max_log_size: int = max_log_size
        self._persistence_handler = persistence_handler
        self._running: bool = False

    def set_persistence_handler(self, handler: EventPersistenceHandler | None) -> None:
        """Set the durable event sink used before subscriber delivery."""
        self._persistence_handler = handler

    def on(self, event_type: str) -> Callable[[EventHandler], EventHandler]:
        """Decorator: subscribe a handler to an event type.

        Usage:
            @bus.on("conversation.turn.completed")
            async def handle(event): ...
        """

        def decorator(handler: EventHandler) -> EventHandler:
            self.subscribe(event_type, handler)
            return handler

        return decorator

    def on_any(self) -> Callable[[EventHandler], EventHandler]:
        """Decorator: subscribe a handler to ALL events."""

        def decorator(handler: EventHandler) -> EventHandler:
            self._wildcard_subscribers.append(handler)
            return handler

        return decorator

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """Subscribe a handler to a specific event type."""
        self._subscribers[event_type].append(handler)
        logger.debug("EventBus[%s]: subscribed to '%s'", self.name, event_type)

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        """Remove a subscription."""
        subs = self._subscribers.get(event_type, [])
        if handler in subs:
            subs.remove(handler)

    async def publish(self, event: BaseEvent) -> None:
        """Publish an event to all matching subscribers.

        Handlers are called concurrently. A slow handler does not
        block other handlers. Exceptions in handlers are logged
        but do not prevent other handlers from running.

        The event is also recorded in the event log for replay/debugging.
        """
        event_type = event.event_type
        handlers = self._subscribers.get(event_type, []) + self._wildcard_subscribers

        # Record every event, even when no component currently subscribes to it.
        self._event_log.append(event)
        if len(self._event_log) > self._max_log_size:
            self._event_log = self._event_log[-self._max_log_size :]

        # Persistence is deliberately fail-closed. Delivering an event that was not
        # durably recorded would break causality, recovery, and deletion semantics.
        if self._persistence_handler:
            await self._persistence_handler(event)

        if not handlers:
            logger.debug(
                "EventBus[%s]: no subscribers for '%s'",
                self.name,
                event_type,
            )
            return

        # Call all handlers concurrently
        async def safe_call(handler: EventHandler, evt: BaseEvent) -> None:
            try:
                result = handler(evt)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception(
                    "EventBus[%s]: handler error for '%s'",
                    self.name,
                    event_type,
                )

        await asyncio.gather(*(safe_call(h, event) for h in handlers))

    async def replay(self, handler: EventHandler, event_types: list[str] | None = None) -> int:
        """Replay logged events through a handler. Returns count replayed."""
        count = 0
        for event in self._event_log:
            if event_types is None or event.event_type in event_types:
                try:
                    result = handler(event)
                    if asyncio.iscoroutine(result):
                        await result
                    count += 1
                except Exception:
                    logger.exception("Replay handler error for %s", event.event_type)
        return count

    @property
    def subscriber_count(self) -> int:
        return sum(len(v) for v in self._subscribers.values()) + len(self._wildcard_subscribers)

    @property
    def event_types_subscribed(self) -> list[str]:
        return sorted(self._subscribers.keys())

    async def shutdown(self) -> None:
        """Clear subscribers and stop accepting events."""
        self._running = False
        self._subscribers.clear()
        self._wildcard_subscribers.clear()
