"""Perception provider interface — local environment sensing.

Privacy-first design:
- Raw screen content NEVER leaves the local machine without consent
- Events are de-identified before being published
- Sensitive regions (passwords, payments, private chats) are masked
- Visual analysis is only performed when policy deems it valuable
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any

from companion.providers.base import Provider, ProviderHealth, ProviderInfo


@dataclass
class ScreenContext:
    """Current screen/window context (de-identified)."""

    app_name: str = ""
    app_category: str = "unknown"
    window_title_deidentified: str = ""
    is_fullscreen: bool = False
    has_sensitive_content: bool = False
    detected_text_categories: list[str] = field(default_factory=list)


@dataclass
class UserActivity:
    """Current user activity state."""

    state: str = "idle"  # active_typing, active_clicking, idle, away, gaming, fullscreen
    idle_duration_ms: int = 0
    input_intensity: float = 0.0
    is_in_meeting: bool = False
    is_in_call: bool = False


@dataclass
class AudioEnvironment:
    """Current audio environment (local analysis only)."""

    has_background_music: bool = False
    has_conversation: bool = False
    noise_level: float = 0.0  # 0 to 1
    user_speaking: bool = False


@dataclass
class PerceptionSnapshot:
    """A point-in-time snapshot of the user's environment."""

    timestamp_ms: int = 0
    screen: ScreenContext = field(default_factory=ScreenContext)
    activity: UserActivity = field(default_factory=UserActivity)
    audio: AudioEnvironment = field(default_factory=AudioEnvironment)
    calendar_events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PerceptionConfig:
    """Configuration for what the perception service monitors."""

    monitor_app_focus: bool = True
    monitor_activity: bool = True
    monitor_audio_environment: bool = False  # Off by default — privacy sensitive
    monitor_calendar: bool = False
    monitor_media_playback: bool = True

    # Sensitive regions to always mask
    masked_window_classes: list[str] = field(
        default_factory=lambda: ["*password*", "*banking*", "*payment*"]
    )
    masked_app_names: list[str] = field(default_factory=list)

    # Quiet hours (no proactive behavior)
    quiet_hours_start: str = "23:00"
    quiet_hours_end: str = "07:00"

    # Sampling
    app_focus_poll_ms: int = 1000
    activity_poll_ms: int = 500
    visual_sampling_enabled: bool = False  # Requires explicit opt-in


class PerceptionProvider(Provider):
    """Abstract interface for environment perception.

    Implementations:
    - WindowsPerceptionProvider: Windows-specific (UIA, window hooks)
    - NullPerceptionProvider: no sensing (privacy mode)
    """

    @abstractmethod
    async def start(self, config: PerceptionConfig) -> None:
        """Start perception with the given configuration."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop all perception."""
        ...

    @abstractmethod
    async def get_snapshot(self) -> PerceptionSnapshot:
        """Get a current snapshot of the environment."""
        ...

    @abstractmethod
    async def request_visual_analysis(self, region: str | None = None) -> dict[str, Any] | None:
        """Request visual analysis of current screen (requires consent).

        Args:
            region: Specific region to analyze (None = active window)

        Returns analysis results or None if consent denied.
        """
        ...

    @abstractmethod
    async def update_config(self, config: PerceptionConfig) -> None:
        """Update perception configuration at runtime."""
        ...

    @abstractmethod
    async def is_quiet_hours(self) -> bool:
        """Check if current time is within quiet hours."""
        ...

    @abstractmethod
    def provider_info(self) -> ProviderInfo: ...

    @abstractmethod
    async def health_check(self) -> ProviderHealth: ...

    @abstractmethod
    async def shutdown(self) -> None: ...
