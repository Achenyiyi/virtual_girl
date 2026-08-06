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
import inspect
import logging
import threading
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from concurrent.futures import Future as ConcurrentFuture

from companion.async_util import wait_with_timeout
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
        handler_timeout_seconds: float = 10.0,
    ) -> None:
        if max_log_size < 1:
            raise ValueError("max_log_size must be positive")
        if handler_timeout_seconds <= 0:
            raise ValueError("handler_timeout_seconds must be positive")
        self.name = name
        self._subscribers: dict[str, list[EventHandler]] = defaultdict(list)
        self._wildcard_subscribers: list[EventHandler] = []
        self._event_log: deque[BaseEvent] = deque(maxlen=max_log_size)
        self._max_log_size: int = max_log_size
        self._persistence_handler = persistence_handler
        self._handler_timeout_seconds = handler_timeout_seconds
        self._publish_lock = asyncio.Lock()
        self._closed = False

    def set_persistence_handler(self, handler: EventPersistenceHandler | None) -> None:
        """Set the durable event sink used before subscriber delivery."""
        self._ensure_open()
        self._persistence_handler = handler

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError(f"EventBus[{self.name}] is shut down")

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
            self._ensure_open()
            self._wildcard_subscribers.append(handler)
            return handler

        return decorator

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """Subscribe a handler to a specific event type."""
        self._ensure_open()
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
        publish_task = asyncio.create_task(self._publish_committed(event))
        try:
            await asyncio.shield(publish_task)
        except asyncio.CancelledError:
            await publish_task
            raise

    async def _publish_committed(self, event: BaseEvent) -> None:
        """Commit one accepted event without cancellation leaving an ambiguous result."""
        async with self._publish_lock:
            if self._closed:
                raise RuntimeError(f"EventBus[{self.name}] is shut down")
            event_type = event.event_type
            handlers = tuple(
                self._subscribers.get(event_type, []) + self._wildcard_subscribers
            )

            # Persistence is deliberately fail-closed. Only committed events enter
            # the replay log or reach subscribers.
            if self._persistence_handler:
                await self._persistence_handler(event)
            self._event_log.append(event)

            if not handlers:
                logger.debug(
                    "EventBus[%s]: no subscribers for '%s'",
                    self.name,
                    event_type,
                )
                return

            async def safe_call(handler: EventHandler, evt: BaseEvent) -> None:
                try:
                    handler_task = asyncio.create_task(self._invoke_handler(handler, evt))
                    if not await wait_with_timeout(
                        handler_task, self._handler_timeout_seconds
                    ):
                        logger.error(
                            "EventBus[%s]: handler timed out for '%s' after %.1fs",
                            self.name,
                            event_type,
                            self._handler_timeout_seconds,
                        )
                        self._quarantine_handler(event_type, handler)
                        return
                    await handler_task
                except asyncio.CancelledError:
                    logger.error(
                        "EventBus[%s]: handler cancelled itself for '%s'",
                        self.name,
                        event_type,
                    )
                except Exception:
                    logger.exception(
                        "EventBus[%s]: handler error for '%s'",
                        self.name,
                        event_type,
                    )

            await asyncio.gather(*(safe_call(h, event) for h in handlers))

    @staticmethod
    async def _invoke_handler(handler: EventHandler, event: BaseEvent) -> None:
        result: object
        if inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(
            type(handler).__call__
        ):
            result = handler(event)
        else:
            result = await EventBus._invoke_sync_handler(handler, event)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    async def _invoke_sync_handler(handler: EventHandler, event: BaseEvent) -> object:
        result_future: ConcurrentFuture[object] = ConcurrentFuture()

        def invoke() -> None:
            if not result_future.set_running_or_notify_cancel():
                return
            try:
                result_future.set_result(handler(event))
            except BaseException as exc:
                result_future.set_exception(exc)

        # A dedicated thread per invocation, so a blocking handler still begins
        # executing even if the bus times it out and quarantines it immediately.
        threading.Thread(
            target=invoke,
            name=f"event-handler-{event.event_type}",
            daemon=True,
        ).start()
        return await asyncio.wrap_future(result_future)

    def _quarantine_handler(self, event_type: str, handler: EventHandler) -> None:
        subscribers = self._subscribers.get(event_type, [])
        if handler in subscribers:
            subscribers.remove(handler)
        if handler in self._wildcard_subscribers:
            self._wildcard_subscribers.remove(handler)

    async def replay(self, handler: EventHandler, event_types: list[str] | None = None) -> int:
        """Replay logged events through a handler. Returns count replayed."""
        async with self._publish_lock:
            self._ensure_open()
            events = tuple(self._event_log)
        count = 0
        for event in events:
            if event_types is None or event.event_type in event_types:
                handler_task = asyncio.create_task(self._invoke_handler(handler, event))
                try:
                    if not await wait_with_timeout(
                        handler_task, self._handler_timeout_seconds
                    ):
                        logger.error(
                            "Replay handler timed out for %s after %.1fs",
                            event.event_type,
                            self._handler_timeout_seconds,
                        )
                        continue
                    await handler_task
                    count += 1
                except asyncio.CancelledError:
                    if not handler_task.done():
                        raise
                    logger.error("Replay handler cancelled itself for %s", event.event_type)
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
        shutdown_task = asyncio.create_task(self._shutdown_committed())
        try:
            await asyncio.shield(shutdown_task)
        except asyncio.CancelledError:
            await shutdown_task
            raise

    async def _shutdown_committed(self) -> None:
        async with self._publish_lock:
            self._closed = True
            self._subscribers.clear()
            self._wildcard_subscribers.clear()
            self._persistence_handler = None
