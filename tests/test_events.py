"""Tests for the event schema system.

Phase 0 acceptance criteria:
- Event serialization/deserialization round-trips
- Event immutability (frozen)
- Event ID generation uniqueness
- Event type registration
- Schema validation rejects invalid data
- Source tracing chains work correctly
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from companion.events.action import (
    ActionRequestedEvent,
    ToolAuditEvent,
)
from companion.events.base import (
    EventHeader,
    generate_ulid,
)
from companion.events.conversation import (
    AsrFinalizedEvent,
    AudioPlayedEvent,
    ConversationTurnCompletedEvent,
    ConversationTurnFailedEvent,
    ConversationTurnInterruptedEvent,
    ConversationTurnStartedEvent,
    LlmResponseGeneratedEvent,
    TtsSynthesizedEvent,
)
from companion.events.emotion import (
    AffectStateUpdatedEvent,
    RelationshipStateUpdatedEvent,
)
from companion.events.memory import (
    FactExtractedEvent,
    FactUpdatedEvent,
    MemoryForgottenEvent,
)
from companion.events.registry import get_registry
from companion.events.shared_experience import (
    MilestoneReachedEvent,
    SharedExperienceCompletedEvent,
)


class TestULIDGeneration:
    """ULID IDs must be unique and sortable."""

    def test_generates_non_empty_string(self):
        ulid = generate_ulid()
        assert ulid
        assert len(ulid) == 26

    def test_generates_unique_ids(self):
        ids = {generate_ulid() for _ in range(1000)}
        assert len(ids) == 1000

    def test_ids_are_time_sortable(self):
        """IDs generated later should sort after earlier ones."""
        id1 = generate_ulid()
        id2 = generate_ulid()
        # They should differ; due to random component, exact ordering
        # isn't guaranteed for same-millisecond generation, but they
        # should at least start with the same timestamp prefix
        assert id1[:8] <= id2[:8]  # Timestamp prefix should be non-decreasing


class TestEventHeader:
    """Event headers must validate and carry required metadata."""

    def test_minimal_header_creates(self):
        header = EventHeader(event_type="test.basic.event")
        assert header.event_id
        assert header.event_type == "test.basic.event"
        assert header.schema_version == 1

    def test_invalid_event_type_rejected(self):
        with pytest.raises(ValidationError):
            EventHeader(event_type="invalid format")

    def test_content_hash_validation(self):
        with pytest.raises(ValidationError):
            EventHeader(event_type="test.basic.event", content_hash="too_short")


class TestBaseEvent:
    """Base event must be frozen (immutable)."""

    def test_events_are_frozen(self):
        event = ConversationTurnStartedEvent(
            header=EventHeader(event_type="conversation.turn.started"),
            session_id="sess_001",
            turn_id="turn_001",
            turn_sequence=0,
        )
        with pytest.raises(ValidationError):
            event.session_id = "new_value"  # type: ignore[misc]

    def test_event_type_auto_populated(self):
        event = ConversationTurnStartedEvent(
            header=EventHeader(event_type="conversation.turn.started"),
            session_id="sess_001",
            turn_id="turn_001",
            turn_sequence=0,
        )
        assert event.event_type == "conversation.turn.started"

    def test_content_hash_computation(self):
        event = ConversationTurnStartedEvent(
            header=EventHeader(event_type="conversation.turn.started"),
            session_id="sess_001",
            turn_id="turn_001",
            turn_sequence=0,
        )
        h = event.compute_content_hash()
        assert len(h) == 64  # SHA-256 hex

    def test_with_hash_populates(self):
        event = ConversationTurnStartedEvent(
            header=EventHeader(event_type="conversation.turn.started"),
            session_id="sess_001",
            turn_id="turn_001",
            turn_sequence=0,
        )
        hashed = event.with_hash()
        assert hashed.header.content_hash
        assert len(hashed.header.content_hash) == 64


class TestConversationEvents:
    """Conversation event lifecycle: started → ASR → LLM → TTS → audio → completed."""

    def test_full_turn_lifecycle(self):
        session_id = "sess_test"
        turn_id = "turn_test"

        # Started
        started = ConversationTurnStartedEvent(
            header=EventHeader(event_type="conversation.turn.started"),
            session_id=session_id,
            turn_id=turn_id,
            turn_sequence=1,
        )
        assert started.turn_id == turn_id

        # ASR
        asr = AsrFinalizedEvent(
            header=EventHeader(event_type="conversation.asr.finalized"),
            turn_id=turn_id,
            segment_index=0,
            transcript="你好",
            confidence=0.95,
            latency_ms=150,
        )
        assert asr.transcript == "你好"

        # LLM
        llm = LlmResponseGeneratedEvent(
            header=EventHeader(event_type="conversation.llm.response"),
            turn_id=turn_id,
            response_text="你好呀！",
            model_id="test-model",
            time_to_first_token_ms=200,
            total_latency_ms=500,
            token_count=10,
        )
        assert llm.response_text == "你好呀！"

        # TTS
        tts = TtsSynthesizedEvent(
            header=EventHeader(event_type="conversation.tts.synthesized"),
            turn_id=turn_id,
            segment_index=0,
            text="你好呀！",
            audio_duration_ms=800,
            time_to_first_byte_ms=300,
            tts_provider="cosyvoice",
        )
        assert tts.tts_provider == "cosyvoice"

        # Audio played
        played = AudioPlayedEvent(
            header=EventHeader(event_type="conversation.audio.played"),
            turn_id=turn_id,
            segment_index=0,
            audio_hash="a" * 64,
            played_duration_ms=800,
        )
        assert not played.was_interrupted

        # Completed
        completed = ConversationTurnCompletedEvent(
            header=EventHeader(event_type="conversation.turn.completed"),
            turn_id=turn_id,
            session_id=session_id,
            turn_sequence=1,
            user_text="你好",
            companion_text="你好呀！",
            total_latency_ms=950,
        )
        assert completed.is_complete

    def test_interruption_flow(self):
        turn_id = "turn_interrupted"
        interrupted = ConversationTurnInterruptedEvent(
            header=EventHeader(event_type="conversation.turn.interrupted"),
            turn_id=turn_id,
            interrupted_at_audio_ms=300,
            new_turn_id="turn_new",
        )
        assert interrupted.interrupted_at_audio_ms == 300

    def test_failed_turn_round_trip_and_stage_validation(self):
        failed = ConversationTurnFailedEvent(
            turn_id="turn_failed",
            session_id="sess_test",
            turn_sequence=2,
            stage="generation",
            error_type="TimeoutError",
            retryable=True,
            elapsed_ms=250,
        )

        restored = get_registry().deserialize(
            failed.event_type,
            failed.model_dump(mode="json"),
        )

        assert isinstance(restored, ConversationTurnFailedEvent)
        assert restored.stage == "generation"
        for stage in ("asr", "tts", "playback"):
            assert ConversationTurnFailedEvent(
                turn_id=f"turn_{stage}",
                session_id="sess_test",
                turn_sequence=3,
                stage=stage,  # type: ignore[arg-type]
                elapsed_ms=0,
            ).stage == stage
        with pytest.raises(ValidationError):
            ConversationTurnFailedEvent(
                turn_id="turn_invalid",
                session_id="sess_test",
                turn_sequence=3,
                stage="unknown",  # type: ignore[arg-type]
                elapsed_ms=0,
            )


class TestSharedExperienceEvents:
    """Shared experience events as defined in PLAN Section 6.1."""

    def test_shared_experience_completed(self):
        event = SharedExperienceCompletedEvent(
            header=EventHeader(event_type="shared_experience.completed"),
            activity_id="act_001",
            activity_type="finished_game_chapter",
            activity_label="完成了游戏第三章",
            user_reaction="excited",
            companion_reaction="proud",
            significance=0.7,
            tags=["gaming", "achievement"],
        )
        assert event.activity_type == "finished_game_chapter"
        assert event.user_reaction == "excited"
        assert event.significance == 0.7

    def test_milestone_reached(self):
        event = MilestoneReachedEvent(
            header=EventHeader(event_type="shared_experience.milestone"),
            milestone_id="mile_001",
            milestone_type="days_together",
            milestone_value=30,
        )
        assert event.milestone_type == "days_together"
        assert not event.commemorated


class TestMemoryEvents:
    """Memory events with source tracing and validity ranges."""

    def test_fact_extracted_with_source(self):
        event = FactExtractedEvent(
            header=EventHeader(event_type="memory.fact.extracted"),
            fact_id="fact_001",
            key="user_favorite_color",
            value="blue",
            confidence=0.9,
            valid_from=datetime.now(UTC),
            source_event_ids=["evt_001", "evt_002"],
        )
        assert event.key == "user_favorite_color"
        assert len(event.source_event_ids) == 2

    def test_fact_updated_preserves_history(self):
        event = FactUpdatedEvent(
            header=EventHeader(event_type="memory.fact.updated"),
            old_fact_id="fact_001",
            new_fact_id="fact_002",
            key="user_favorite_color",
            old_value="blue",
            new_value="green",
            reason="user_correction",
        )
        assert event.old_value == "blue"
        assert event.new_value == "green"

    def test_memory_forgotten_with_cascade(self):
        event = MemoryForgottenEvent(
            header=EventHeader(event_type="memory.forgotten"),
            event_ids_to_delete=["evt_001"],
            cascade_count=5,
        )
        assert event.cascade_count == 5


class TestEventRegistry:
    """Event type registry must discover and validate all built-in types."""

    def test_registry_contains_all_types(self):
        registry = get_registry()
        types = registry.list_types()
        expected_prefixes = [
            "conversation.turn.",
            "conversation.asr.",
            "conversation.llm.",
            "conversation.tts.",
            "conversation.audio.",
            "memory.fact.",
            "memory.episode.",
            "memory.reflection.",
            "memory.forgotten",
            "memory.rebuilt",
            "emotion.affect.",
            "emotion.relationship.",
            "emotion.expression",
            "shared_experience.",
            "perception.",
            "action.",
            "lifecycle.",
        ]
        for prefix in expected_prefixes:
            matching = [t for t in types if t.startswith(prefix)]
            assert matching, f"No events found for prefix '{prefix}'"

    def test_registry_deserialize_round_trip(self):
        registry = get_registry()
        data = {
            "header": {
                "event_id": "evt_test",
                "event_type": "conversation.turn.started",
                "privacy": "private",
                "severity": "info",
                "source": {},
            },
            "session_id": "sess_test",
            "turn_id": "turn_test",
            "turn_sequence": 0,
        }
        event = registry.deserialize("conversation.turn.started", data)
        assert isinstance(event, ConversationTurnStartedEvent)
        assert event.session_id == "sess_test"

    def test_unknown_event_type_raises(self):
        registry = get_registry()
        with pytest.raises(KeyError):
            registry.deserialize("nonexistent.event.type", {})


class TestAffectStateEvents:
    """Emotion events with bounded deltas."""

    def test_affect_updated_with_bounds(self):
        event = AffectStateUpdatedEvent(
            header=EventHeader(event_type="emotion.affect.updated"),
            valence=0.5,
            arousal=0.6,
            trust=0.7,
            closeness=0.4,
            energy=0.8,
            uncertainty=0.2,
            delta_valence=0.1,
        )
        assert event.valence == 0.5
        assert -0.3 <= event.delta_valence <= 0.3  # Bounded

    def test_relationship_updated_requires_evidence(self):
        event = RelationshipStateUpdatedEvent(
            header=EventHeader(event_type="emotion.relationship.updated"),
            trust=0.6,
            evidence_event_ids=["evt_001"],
        )
        assert event.trust == 0.6


class TestActionEvents:
    """Action events with risk classification and audit trail."""

    def test_action_request_with_risk(self):
        event = ActionRequestedEvent(
            header=EventHeader(event_type="action.requested"),
            action_id="act_001",
            action_type="search_web",
            method="dom",
            risk_level="reversible_low",
            requires_confirmation=False,
            requested_by="companion_orchestrator",
        )
        assert event.risk_level == "reversible_low"

    def test_tool_audit_records_invocation(self):
        event = ToolAuditEvent(
            header=EventHeader(event_type="action.tool_audit"),
            tool_name="search_web",
            risk_level="reversible_low",
            permission_checked=True,
            caller_component="action_service",
            user_present=True,
        )
        assert event.permission_checked
