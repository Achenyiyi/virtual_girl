"""Lifecycle events — companion startup, shutdown, and session management."""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from companion.events.base import BaseEvent


class CompanionStartupEvent(BaseEvent):
    """The companion runtime started up."""

    __event_type__: ClassVar[str] = "lifecycle.companion.started"

    version: str = Field(..., description="Companion software version")
    config_hash: str = Field(default="", description="Hash of active configuration")
    environment: str = Field(
        default="production", description="'development', 'staging', 'production'"
    )


class CompanionShutdownEvent(BaseEvent):
    """The companion runtime is shutting down cleanly."""

    __event_type__: ClassVar[str] = "lifecycle.companion.stopped"

    reason: str = Field(
        default="user_request", description="'user_request', 'crash', 'update', 'system_shutdown'"
    )
    uptime_seconds: int = Field(default=0, ge=0)
    active_session_count: int = Field(default=0, ge=0)
    unsaved_event_count: int = Field(default=0, ge=0)


class SessionStartedEvent(BaseEvent):
    """A new interaction session began (user opened the app/woke companion)."""

    __event_type__: ClassVar[str] = "lifecycle.session.started"

    session_id: str = Field(..., description="Session identifier (ULID)")
    start_reason: str = Field(
        default="user_activated",
        description="'user_activated', 'scheduled_wake', 'event_triggered'",
    )
    time_since_last_session_seconds: int | None = Field(default=None)


class SessionEndedEvent(BaseEvent):
    """An interaction session ended."""

    __event_type__: ClassVar[str] = "lifecycle.session.ended"

    session_id: str = Field(...)
    end_reason: str = Field(
        default="user_closed", description="'user_closed', 'timeout', 'crash', 'quiet_hours'"
    )
    turn_count: int = Field(default=0, ge=0)
    duration_seconds: int = Field(default=0, ge=0)
    user_satisfaction_rating: int | None = Field(default=None, ge=1, le=5)
