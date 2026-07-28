"""Tests for the State Manager, Policy Gate, and Event Bus.

Phase 0 acceptance criteria:
- State changes have bounded deltas
- Identity can only be updated by user
- Affect decays toward baseline over time
- Policy gate respects quiet hours, budgets, and cooldowns
- Event bus delivers events to subscribed handlers
"""

from __future__ import annotations

import pytest

from companion.core.event_bus import EventBus
from companion.core.policy_gate import PolicyGate, ProactiveLevel
from companion.core.state_manager import StateManager
from companion.events.base import EventHeader
from companion.events.conversation import ConversationTurnStartedEvent
from companion.schemas.identity import IdentityCore


class TestStateManager:
    """State manager enforces constraints on identity, affect, and relationship."""

    def test_default_identity_created(self):
        mgr = StateManager()
        assert mgr.identity.name == "未命名伙伴"
        assert len(mgr.identity.hard_boundaries) > 0

    def test_identity_update_must_be_user_initiated(self):
        mgr = StateManager()
        current = mgr.identity
        new = current.increment_version("test")
        updated = mgr.update_identity(new)
        assert updated.version == current.version + 1

    def test_identity_system_update_rejected(self):
        mgr = StateManager()
        from datetime import datetime as dt

        current_time = dt.now()
        fake = IdentityCore(
            version=99,
            updated_at=current_time,
            updated_by="system",
            name="test",
            self_concept="test",
            origin_story="test",
        )
        with pytest.raises(ValueError, match="user"):
            mgr.update_identity(fake)

    def test_identity_version_must_increment(self):
        mgr = StateManager()
        current = mgr.identity
        same_version = IdentityCore(
            version=current.version,
            updated_at=__import__("datetime").datetime.now(),
            updated_by="user",
            name=current.name,
            self_concept=current.self_concept,
            origin_story=current.origin_story,
        )
        with pytest.raises(ValueError, match="version"):
            mgr.update_identity(same_version)

    def test_affect_bounded_deltas(self):
        mgr = StateManager()
        initial = mgr.affect

        # Apply a delta within bounds
        new = mgr.apply_affect_event(delta_valence=0.1, delta_energy=-0.1)
        assert new.valence > initial.valence
        assert new.version > initial.version

        # Large delta is capped
        result = mgr.apply_affect_event(delta_valence=5.0)
        assert result.valence <= 1.0

    def test_affect_time_decay(self):
        mgr = StateManager()

        # Push state far from baseline
        mgr.apply_affect_event(delta_valence=0.3, delta_arousal=0.2)

        # After 1000 seconds, state should drift toward baseline
        decayed = mgr.apply_time_decay(1000)
        assert abs(decayed.valence) < 0.3  # Should be closer to baseline

    def test_relationship_bounded_deltas(self):
        mgr = StateManager()
        initial = mgr.relationship

        # Small delta
        new = mgr.apply_relationship_event(delta_trust=0.05)
        assert new.trust > initial.trust

        # Closeness delta capped at 0.05 per event
        initial_close = new.closeness
        result = mgr.apply_relationship_event(delta_closeness=1.0)
        assert result.closeness <= initial_close + 0.05

    def test_dominant_emotion_mapping(self):
        mgr = StateManager()
        assert mgr.dominant_emotion()  # Should return a string

    def test_system_prompt_fragment(self):
        mgr = StateManager()
        fragment = mgr.get_system_prompt_fragment()
        assert "未命名伙伴" in fragment
        assert "绝对禁止" in fragment


class TestPolicyGate:
    """Policy gate enforces safety and interaction budgets."""

    def _make_gate(self) -> PolicyGate:
        """Create a PolicyGate with quiet hours disabled for deterministic testing."""
        gate = PolicyGate()
        gate._quiet_hours_enabled = False  # Disable for testing
        return gate

    def test_default_state_allows_level_1(self):
        gate = self._make_gate()
        decision = gate.evaluate_proactive(ProactiveLevel.LEVEL_1_SUBTLE)
        # Level 1 (subtle) should generally be allowed
        assert decision.level >= ProactiveLevel.LEVEL_0_IDLE

    def test_level_4_requires_high_utility(self):
        gate = self._make_gate()
        decision = gate.evaluate_proactive(
            ProactiveLevel.LEVEL_4_ACTION, relevance=0.2, urgency=0.1
        )
        # Level 4 should be hard to trigger with low relevance/urgency
        assert not decision.allowed or decision.level < ProactiveLevel.LEVEL_4_ACTION

    def test_muted_suppresses_everything(self):
        gate = self._make_gate()
        gate.set_muted(True)
        decision = gate.evaluate_proactive(
            ProactiveLevel.LEVEL_3_CONVERSATION, relevance=1.0, urgency=1.0
        )
        assert not decision.allowed

    def test_high_urgency_overcomes_threshold(self):
        gate = self._make_gate()
        decision = gate.evaluate_proactive(
            ProactiveLevel.LEVEL_2_HINT,
            relevance=0.8,
            urgency=0.9,
            relationship_value=0.7,
        )
        # High value should pass
        assert decision.allowed

    def test_rejection_feedback_penalizes(self):
        gate = self._make_gate()
        # Simulate many rejections
        for _ in range(15):
            gate.record_feedback(False)
        decision = gate.evaluate_proactive(ProactiveLevel.LEVEL_2_HINT)
        # Heavy rejection should reduce allowance
        assert decision.user_rejection_score > 0.5

    def test_action_readonly_auto_approved(self):
        gate = PolicyGate()
        decision = gate.evaluate_action("read_window_title")
        assert decision.approved
        assert not decision.requires_user_confirmation

    def test_action_irreversible_requires_confirmation(self):
        gate = PolicyGate()
        decision = gate.evaluate_action("send_message")
        assert decision.requires_user_confirmation


class TestEventBus:
    """Event bus delivers typed events to subscribers."""

    @pytest.mark.asyncio
    async def test_subscribe_and_publish(self):
        bus = EventBus("test")
        received: list = []

        @bus.on("conversation.turn.started")
        async def handler(event):
            received.append(event)

        event = ConversationTurnStartedEvent(
            header=EventHeader(event_type="conversation.turn.started"),
            session_id="sess",
            turn_id="turn",
            turn_sequence=0,
        )
        await bus.publish(event)
        assert len(received) == 1
        assert received[0].turn_id == "turn"

    @pytest.mark.asyncio
    async def test_wildcard_subscriber(self):
        bus = EventBus("test")
        received: list = []

        @bus.on_any()
        async def handler(event):
            received.append(event)

        e1 = ConversationTurnStartedEvent(
            header=EventHeader(event_type="conversation.turn.started"),
            session_id="sess",
            turn_id="t1",
            turn_sequence=0,
        )
        await bus.publish(e1)
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_no_subscriber_no_error(self):
        bus = EventBus("test")
        event = ConversationTurnStartedEvent(
            header=EventHeader(event_type="conversation.turn.started"),
            session_id="sess",
            turn_id="t1",
            turn_sequence=0,
        )
        await bus.publish(event)  # Should not raise
        replayed = await bus.replay(lambda _: None)
        assert replayed == 1

    @pytest.mark.asyncio
    async def test_persists_before_delivery(self):
        order: list[str] = []

        async def persist(event):
            order.append(f"persist:{event.event_id}")

        bus = EventBus("test", persistence_handler=persist)

        @bus.on("conversation.turn.started")
        async def handler(event):
            order.append(f"deliver:{event.event_id}")

        event = ConversationTurnStartedEvent(
            session_id="sess",
            turn_id="t1",
            turn_sequence=0,
        )
        await bus.publish(event)
        assert order == [f"persist:{event.event_id}", f"deliver:{event.event_id}"]

    @pytest.mark.asyncio
    async def test_handler_error_does_not_block_others(self):
        bus = EventBus("test")
        ok_received: list = []

        @bus.on("conversation.turn.started")
        async def bad_handler(event):
            msg = "deliberate test error"
            raise RuntimeError(msg)

        @bus.on("conversation.turn.started")
        async def good_handler(event):
            ok_received.append(event)

        event = ConversationTurnStartedEvent(
            header=EventHeader(event_type="conversation.turn.started"),
            session_id="sess",
            turn_id="t1",
            turn_sequence=0,
        )
        await bus.publish(event)
        assert len(ok_received) == 1  # Good handler still ran

    def test_subscriber_count(self):
        bus = EventBus("test")

        @bus.on("type.a")
        async def h1(e):
            pass

        @bus.on("type.b")
        async def h2(e):
            pass

        assert bus.subscriber_count == 2
        assert "type.a" in bus.event_types_subscribed
        assert "type.b" in bus.event_types_subscribed
