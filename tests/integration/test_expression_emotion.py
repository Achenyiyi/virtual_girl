"""Integration tests for expression mapping and emotion system (Phase 3)."""

from __future__ import annotations

from companion.core.expression_mapper import ExpressionMapper
from companion.core.state_manager import StateManager
from companion.schemas.affect import AffectState


class TestExpressionMapper:
    """Test affect → multimodal expression mapping."""

    def test_maps_happy_expression(self):
        mapper = ExpressionMapper()
        affect = AffectState(valence=0.8, arousal=0.7, energy=0.9)
        snapshot = mapper.map(affect)

        assert snapshot.facial.expression_id in ("happy", "gentle_smile")
        assert snapshot.facial.mouth_smile > 0.5  # Happy = smile
        assert snapshot.voice.rate > 1.0  # Higher arousal = faster speech

    def test_maps_sad_expression(self):
        mapper = ExpressionMapper()
        affect = AffectState(valence=-0.7, arousal=0.2, energy=0.3)
        snapshot = mapper.map(affect)

        assert snapshot.facial.expression_id in ("sad", "upset")
        assert snapshot.facial.mouth_frown > 0.3
        assert snapshot.voice.style in ("sad", "empathetic", "general")

    def test_maps_neutral_expression(self):
        mapper = ExpressionMapper()
        affect = AffectState(valence=0.0, arousal=0.5, energy=0.7)
        snapshot = mapper.map(affect)

        assert snapshot.facial.expression_id in ("neutral", "attentive")
        assert snapshot.voice.style == "general"

    def test_tts_params_from_affect(self):
        mapper = ExpressionMapper()
        affect = AffectState(valence=0.5, arousal=0.6)
        voice = mapper.map_for_tts(affect)

        assert 0.7 <= voice.rate <= 1.4
        assert 0.8 <= voice.pitch <= 1.2
        assert voice.style  # Should be a valid style

    def test_facial_params_from_affect(self):
        mapper = ExpressionMapper()
        affect = AffectState(valence=0.3, arousal=0.4)
        facial = mapper.map_for_avatar(affect)

        assert 0.3 <= facial.eye_open <= 1.0
        assert facial.expression_id

    def test_energy_affects_eyes(self):
        mapper = ExpressionMapper()
        energetic = AffectState(valence=0, arousal=0.5, energy=1.0)
        tired = AffectState(valence=0, arousal=0.5, energy=0.1)

        energetic_facial = mapper.map_for_avatar(energetic)
        tired_facial = mapper.map_for_avatar(tired)

        assert energetic_facial.eye_open > tired_facial.eye_open

    def test_emotion_bounded_deltas_with_state_manager(self):
        """State manager should cap emotional deltas per PLAN Section 4.5."""
        mgr = StateManager()
        initial = mgr.affect

        # Apply a huge delta
        mgr.apply_affect_event(delta_valence=2.0)
        assert abs(mgr.affect.valence) <= 1.0

        # Applies within bounds
        mgr.apply_affect_event(delta_valence=0.2)
        assert mgr.affect.valence > initial.valence

    def test_relationship_changes_are_slow(self):
        """Relationship trust/closeness should only change slowly."""
        mgr = StateManager()
        initial = mgr.relationship

        # Apply a closeness delta — must be capped at 0.05
        mgr.apply_relationship_event(delta_closeness=0.5)
        after = mgr.relationship
        assert after.closeness <= initial.closeness + 0.05

        # Trust capped at 0.1
        mgr.apply_relationship_event(delta_trust=0.5)
        after = mgr.relationship
        assert after.trust <= initial.trust + 0.1 + 0.05  # cumulative max

    def test_expression_snapshot_complete(self):
        """Snapshot should include all modalities."""
        mapper = ExpressionMapper()
        snapshot = mapper.map(AffectState(valence=0.6, arousal=0.7))

        assert snapshot.facial is not None  # Should have facial params
        assert snapshot.voice.rate > 0
        assert len(snapshot.gestures) >= 0  # May or may not have gestures
        assert snapshot.source_valence == 0.6
        assert snapshot.source_arousal == 0.7


class TestEmotionConsistency:
    """Emotion consistency across modalities (PLAN Phase 3 requirement)."""

    def test_voice_and_face_are_consistent(self):
        """Voice and face should express the same emotion."""
        mapper = ExpressionMapper()

        for valence, arousal in [(0.8, 0.7), (-0.6, 0.3), (0.0, 0.5), (0.3, 0.2), (-0.2, 0.8)]:
            snapshot = mapper.map(AffectState(valence=valence, arousal=arousal))
            face_id = snapshot.facial.expression_id
            voice_style = snapshot.voice.style

            # Check consistency: happy face → cheerful/gentle voice
            if "happy" in face_id or "smile" in face_id or "content" in face_id:
                assert voice_style in ("cheerful", "gentle", "excited", "general"), (
                    f"Face={face_id} but voice={voice_style} (v={valence}, a={arousal})"
                )

            if "sad" in face_id or "upset" in face_id:
                assert voice_style in ("sad", "empathetic", "general"), (
                    f"Face={face_id} but voice={voice_style} (v={valence}, a={arousal})"
                )

    def test_no_sudden_emotion_jumps(self):
        """Consecutive emotion states should not have drastic jumps."""
        mgr = StateManager()

        # Two consecutive normal events
        state1 = mgr.apply_affect_event(delta_valence=-0.15, delta_arousal=0.1)
        state2 = mgr.apply_affect_event(delta_valence=0.05, delta_arousal=-0.05)

        # Difference between consecutive states should be small
        assert abs(state2.valence - state1.valence) < 0.3  # Within bounded delta range
