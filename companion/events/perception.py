"""Perception events — local sensor data about the user's environment.

Perception is privacy-first: raw screen content is never sent to models.
Instead, local sensors produce de-identified structured events:
- Current application name/category
- Window title (de-identified)
- Input/idle state
- Media playback status
- Game state (if user has opted in)

Visual understanding (screenshot analysis) is only performed
when the proactive policy determines high value AND user has consented.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from companion.events.base import BaseEvent


class AppFocusChangedEvent(BaseEvent):
    """The user switched to a different application window."""

    __event_type__: ClassVar[str] = "perception.app.focus_changed"

    app_name: str = Field(..., description="Application executable name")
    app_category: str = Field(
        default="unknown",
        description="De-identified category: 'browser', 'game', 'ide', 'office', 'media', etc.",
    )
    window_title_deidentified: str = Field(
        default="",
        description="Window title with personal info removed (project names, URLs sanitized)",
    )
    previous_app_name: str = Field(default="")
    previous_app_category: str = Field(default="unknown")
    duration_in_previous_ms: int = Field(default=0, ge=0)


class UserActivityStateChangedEvent(BaseEvent):
    """User input/idle state changed."""

    __event_type__: ClassVar[str] = "perception.activity.state_changed"

    state: str = Field(
        ...,
        description="'active_typing', 'active_clicking', 'idle', 'away', 'gaming', 'fullscreen'",
    )
    idle_duration_ms: int = Field(default=0, ge=0, description="How long user has been idle")
    is_fullscreen: bool = Field(default=False)
    is_conference_call: bool = Field(default=False)
    input_intensity: float = Field(
        default=0,
        ge=0,
        le=1,
        description="Rate of keyboard/mouse events (0 = idle, 1 = intense)",
    )


class MediaPlaybackEvent(BaseEvent):
    """Media playback detected (music, video, stream)."""

    __event_type__: ClassVar[str] = "perception.media.playback"

    media_type: str = Field(..., description="'music', 'video', 'stream', 'podcast'")
    state: str = Field(..., description="'playing', 'paused', 'stopped'")
    title_deidentified: str = Field(default="", description="Title with personal info removed")
    artist_deidentified: str = Field(default="")
    is_explicit: bool = Field(default=False)
    volume_level: float | None = Field(default=None, ge=0, le=1)


class ScheduleEventDetectedEvent(BaseEvent):
    """A calendar or reminder event was detected."""

    __event_type__: ClassVar[str] = "perception.schedule.detected"

    event_category: str = Field(
        ...,
        description="'meeting', 'birthday', 'deadline', 'reminder', 'commute'",
    )
    title_deidentified: str = Field(default="")
    start_time: str = Field(default="", description="ISO 8601")
    end_time: str | None = Field(default=None)
    is_upcoming: bool = Field(default=False)
    minutes_until: int | None = Field(default=None)
