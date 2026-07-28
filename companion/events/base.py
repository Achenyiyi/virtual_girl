"""Base event types that all domain events inherit from.

The event ledger is append-only and immutable. Every event carries:
- A globally unique event_id (ULID-based for time-sortability)
- A typed header with actor, privacy, and source information
- An occurred_at timestamp in ISO 8601 with timezone

Events MUST NOT be modified after being written. Corrections are represented
as new events that reference the original.
"""

from __future__ import annotations

import hashlib
import os
import time
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ── ULID generator (simple, no dependency) ──────────────────────────────


def generate_ulid() -> str:
    """Generate a ULID-like identifier: timestamp(10) + random(16) = 26 chars.

    Uses milliseconds since Unix epoch (48-bit) + 80 bits of randomness,
    encoded in Crockford base32. This gives us time-sortable unique IDs
    without the `python-ulid` dependency.

    Note: This is a simplified, self-contained implementation. It produces
    26-char Crockford-base32 strings but the timestamp/random split differs
    from the canonical ULID spec. For full spec-compliance, replace with
    the `python-ulid` package. IDs are unique and time-sortable for all
    practical purposes within this project.
    """
    encoding = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32
    ts = int(time.time() * 1000)
    rand = os.urandom(10)

    # Encode timestamp (48 bits → 10 characters)
    ts_chars: list[str] = []
    for _ in range(10):
        ts_chars.append(encoding[ts & 0x1F])
        ts >>= 5
    ts_part = "".join(reversed(ts_chars))

    # Encode random (80 bits → 16 characters)
    rand_part = ""
    rand_int = int.from_bytes(rand, "big")
    for _ in range(16):
        rand_part += encoding[rand_int & 0x1F]
        rand_int >>= 5

    return f"{ts_part}{rand_part}"


# ── Event enums ──────────────────────────────────────────────────────────


class EventPrivacy(StrEnum):
    """Privacy classification for events.

    - private: only visible to the companion and user locally
    - sensitive: requires explicit user consent to process (e.g., passwords)
    - shared: may be used in anonymous telemetry or improvement
    """

    PRIVATE = "private"
    SENSITIVE = "sensitive"
    SHARED = "shared"


class EventSeverity(StrEnum):
    """Severity level for monitoring and filtering."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ActorType(StrEnum):
    """Who or what initiated the event."""

    USER = "user"
    COMPANION = "companion"
    SYSTEM = "system"
    SENSOR = "sensor"
    EXTERNAL = "external"


# ── Event source reference ──────────────────────────────────────────────


class EventSource(BaseModel):
    """Traceable origin of an event — links to prior events or system inputs.

    Every event must declare what triggered it. This enables:
    - Full causality chains for memory reasoning
    - Cascade deletion when a user asks to forget something
    - Audit trails for action safety
    """

    turn_ids: list[str] = Field(
        default_factory=list,
        description="Conversation turn IDs that caused or contributed to this event",
    )
    screen_event_id: str | None = Field(
        default=None,
        description="Perception event that triggered this (e.g., app switch detected)",
    )
    prior_event_ids: list[str] = Field(
        default_factory=list,
        description="Other events this was derived from (e.g., reflection from episodes)",
    )
    triggered_by: ActorType | None = Field(
        default=None,
        description="Who initiated the causal chain",
    )


# ── Event header (common to all events) ──────────────────────────────────


class EventHeader(BaseModel):
    """Header fields shared by every domain event.

    The header carries routing, tracing, and privacy metadata.
    The body (in subclasses) carries the domain-specific payload.
    """

    event_id: str = Field(
        default_factory=generate_ulid,
        description="Globally unique, time-sortable event identifier",
    )
    event_type: str = Field(
        default="system.abstract",
        description="Fully qualified event type string (e.g., 'conversation.turn.completed')",
        pattern=r"^[a-z_]+\.[a-z_]+(\.[a-z_]+)?$",
    )
    occurred_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the event actually happened (wall clock)",
    )
    recorded_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the event was persisted (may differ from occurred_at)",
    )
    actors: list[ActorType] = Field(
        default_factory=lambda: [ActorType.SYSTEM],
        description="Actors involved in this event",
    )
    privacy: EventPrivacy = Field(
        default=EventPrivacy.PRIVATE,
        description="Privacy classification",
    )
    severity: EventSeverity = Field(
        default=EventSeverity.INFO,
        description="Severity level",
    )
    source: EventSource = Field(
        default_factory=EventSource,
        description="What caused this event",
    )
    schema_version: int = Field(
        default=1,
        description="Version of this event's schema for migration support",
        ge=1,
    )
    content_hash: str | None = Field(
        default=None,
        description="SHA-256 of the event payload for integrity verification",
    )

    @field_validator("content_hash")
    @classmethod
    def _hash_must_be_hex(cls, v: str | None) -> str | None:
        if v is not None and len(v) != 64:
            msg = "content_hash must be a 64-character hex string (SHA-256)"
            raise ValueError(msg)
        return v


# ── Base event ───────────────────────────────────────────────────────────


class BaseEvent(BaseModel):
    """Root for all domain events. Immutable (frozen) to enforce append-only.

    Subclass this and override:
    - __event_type__: the fully qualified type string
    - payload fields: domain-specific data
    """

    __event_type__: ClassVar[str] = "system.abstract"
    model_config = ConfigDict(frozen=True)

    header: EventHeader = Field(default_factory=lambda: EventHeader())

    def model_post_init(self, __context: Any) -> None:
        """Auto-populate header fields from class defaults."""
        if not self.header.event_type or self.header.event_type == "system.abstract":
            object.__setattr__(self.header, "event_type", self.__event_type__)

    @property
    def event_id(self) -> str:
        return self.header.event_id

    @property
    def event_type(self) -> str:
        return self.header.event_type

    @property
    def occurred_at(self) -> datetime:
        return self.header.occurred_at

    def compute_content_hash(self) -> str:
        """Compute SHA-256 hash of the event body for integrity verification."""
        payload = self.model_dump_json(exclude={"header"})
        return hashlib.sha256(payload.encode()).hexdigest()

    def with_hash(self: Any) -> Any:
        """Return a copy with content_hash populated."""
        h = self.compute_content_hash()
        new_header = self.header.model_copy(update={"content_hash": h})
        return self.model_copy(update={"header": new_header})
