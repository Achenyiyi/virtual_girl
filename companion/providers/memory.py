"""Memory provider interface — the five-layer memory system.

Layer 1: event_log    — Immutable raw events (append-only)
Layer 2: working_memory — Current session and recent activity (token/time budgeted)
Layer 3: semantic_facts — User preferences, identity, stable facts (with validity range)
Layer 4: episodic_memory — Shared experiences with temporal/causal links
Layer 5: reflections    — Synthesized insights from multiple events

All derived layers must be rebuildable from event_log alone.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from companion.providers.base import Provider, ProviderHealth, ProviderInfo


@dataclass
class EventQuery:
    """Query parameters for searching the event log."""

    event_types: list[str] = field(default_factory=list)
    start_time: datetime | None = None
    end_time: datetime | None = None
    actors: list[str] = field(default_factory=list)
    privacy_levels: list[str] = field(default_factory=list)
    limit: int = 100
    offset: int = 0
    sort_ascending: bool = True


@dataclass
class SemanticFact:
    """A structured fact about the user or relationship."""

    fact_id: str
    key: str
    value: str
    category: str = "general"  # preference, identity, relationship, schedule
    confidence: float = 1.0
    valid_from: datetime | None = None
    valid_to: datetime | None = None  # None = currently valid
    source_event_ids: list[str] = field(default_factory=list)
    extraction_method: str = "llm"


@dataclass
class Episode:
    """An episodic memory of a shared experience."""

    episode_id: str
    title: str
    summary: str
    participants: list[str] = field(default_factory=lambda: ["user", "companion"])
    emotional_salience: float = 0.5
    turn_ids: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    occurred_at: datetime | None = None


@dataclass
class Reflection:
    """A synthesized insight from multiple events or episodes."""

    reflection_id: str
    content: str
    category: str = "general"
    source_event_ids: list[str] = field(default_factory=list)
    source_episode_ids: list[str] = field(default_factory=list)
    confidence: float = 0.5
    generated_plan: str | None = None
    created_at: datetime | None = None


@dataclass
class MemorySearchResult:
    """A result from searching across memory layers."""

    layer: str  # 'event', 'fact', 'episode', 'reflection'
    content: str
    score: float = 0.0
    source_id: str = ""
    timestamp: datetime | None = None

    # For fact results
    key: str = ""
    value: str = ""

    # For episode results
    episode_id: str = ""


class MemoryProvider(Provider):
    """Abstract interface for the five-layer memory system.

    The memory provider is the ONLY path for reading/writing memory.
    All other components go through this interface. This ensures:
    - Single source of truth (event log)
    - Consistent fact validity management
    - Rebuildability from event log
    - Cascade deletion support
    """

    # ── Event Log (Layer 1) ──────────────────────────────────────────

    @abstractmethod
    async def append_event(self, event_data: dict[str, Any]) -> str:
        """Append a raw event to the immutable log. Returns event_id."""
        ...

    @abstractmethod
    async def query_events(self, query: EventQuery) -> list[dict[str, Any]]:
        """Search the event log with filters."""
        ...

    @abstractmethod
    async def get_event(self, event_id: str) -> dict[str, Any] | None:
        """Retrieve a single event by ID."""
        ...

    # ── Semantic Facts (Layer 3) ─────────────────────────────────────

    @abstractmethod
    async def upsert_fact(self, fact: SemanticFact) -> str:
        """Store or update a fact. Updates close old validity, create new entry."""
        ...

    @abstractmethod
    async def get_fact(self, key: str) -> SemanticFact | None:
        """Get the currently-valid fact for a key."""
        ...

    @abstractmethod
    async def search_facts(
        self, query: str, category: str | None = None, limit: int = 10
    ) -> list[SemanticFact]:
        """Semantic (FTS + vector) search over facts."""
        ...

    @abstractmethod
    async def list_fact_updates(self, key: str) -> list[dict[str, Any]]:
        """Get the full update history for a fact key."""
        ...

    # ── Episodic Memory (Layer 4) ────────────────────────────────────

    @abstractmethod
    async def create_episode(self, episode: Episode) -> str:
        """Create a new episodic memory. Returns episode_id."""
        ...

    @abstractmethod
    async def search_episodes(
        self, query: str, limit: int = 10, min_salience: float = 0.0
    ) -> list[Episode]:
        """Search episodic memories semantically."""
        ...

    @abstractmethod
    async def get_episode(self, episode_id: str) -> Episode | None: ...

    # ── Reflections (Layer 5) ────────────────────────────────────────

    @abstractmethod
    async def create_reflection(self, reflection: Reflection) -> str:
        """Store a synthesized reflection."""
        ...

    @abstractmethod
    async def get_recent_reflections(self, limit: int = 10) -> list[Reflection]: ...

    # ── Memory Management ────────────────────────────────────────────

    @abstractmethod
    async def forget(self, event_ids: list[str], reason: str = "user_request") -> int:
        """Cascade-delete events and all derived memory. Returns cascade count."""
        ...

    @abstractmethod
    async def rebuild_from_log(self) -> dict[str, Any]:
        """Rebuild all derived memory from the event log.

        Returns stats: event_count, facts_restored, episodes_restored,
        reflections_restored, consistency_errors, passed_check.
        """
        ...

    @abstractmethod
    async def verify_consistency(self) -> dict[str, Any]:
        """Check that derived memory is consistent with event log.

        Returns: is_consistent, error_count, error_details.
        """
        ...

    # ── Provider Lifecycle ───────────────────────────────────────────

    @abstractmethod
    def provider_info(self) -> ProviderInfo: ...

    @abstractmethod
    async def health_check(self) -> ProviderHealth: ...

    @abstractmethod
    async def shutdown(self) -> None: ...
