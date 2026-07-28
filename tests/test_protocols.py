"""Tests for the turn management and audio confirmation protocols.

Phase 0 acceptance criteria:
- Turn IDs are validated
- State transitions follow the allowed graph
- Interruption semantics are correct
- Audio confirmation tracks what was actually played
- Only played audio text enters shared history
"""

from __future__ import annotations

from companion.protocols.turn import TurnManager, TurnProtocol, TurnState


class TestTurnProtocol:
    """Turn protocol defines valid state transitions."""

    def test_valid_transitions(self):
        assert TurnProtocol.can_transition(TurnState.STARTED, TurnState.ASR_PROCESSING)
        assert TurnProtocol.can_transition(TurnState.ASR_PROCESSING, TurnState.LLM_THINKING)
        assert TurnProtocol.can_transition(TurnState.LLM_THINKING, TurnState.TTS_SYNTHESIZING)
        assert TurnProtocol.can_transition(TurnState.TTS_SYNTHESIZING, TurnState.PLAYING)
        assert TurnProtocol.can_transition(TurnState.TTS_SYNTHESIZING, TurnState.COMPLETED)
        assert TurnProtocol.can_transition(TurnState.PLAYING, TurnState.COMPLETED)

    def test_interruption_from_any_active_state(self):
        """Interruption should be valid from LLM, TTS, and PLAYING states."""
        for state in [TurnState.LLM_THINKING, TurnState.TTS_SYNTHESIZING, TurnState.PLAYING]:
            assert TurnProtocol.can_transition(state, TurnState.INTERRUPTED), (
                f"Should allow interrupt from {state}"
            )

    def test_terminal_states_no_transitions(self):
        for state in [
            TurnState.COMPLETED,
            TurnState.INTERRUPTED,
            TurnState.ERROR,
            TurnState.CANCELLED,
        ]:
            for target in TurnState:
                assert not TurnProtocol.can_transition(state, target)

    def test_validate_turn_id(self):
        assert TurnProtocol.validate_turn_id("turn_01HXYZ1234567890ABCDEF")
        assert not TurnProtocol.validate_turn_id("")
        assert not TurnProtocol.validate_turn_id("short")

    def test_validate_session_id(self):
        assert TurnProtocol.validate_session_id("sess_01HXYZ1234567890ABCDEF")
        assert not TurnProtocol.validate_session_id("")


class TestTurnManager:
    """Turn manager handles lifecycle and interruption."""

    def test_create_turn(self):
        mgr = TurnManager()
        turn = mgr.create_turn("sess_01HXYZ1234567890ABCDEF", 0)
        assert turn.turn_id
        assert turn.state == TurnState.STARTED
        assert turn.turn_sequence == 0
        assert turn.is_active

    def test_state_transitions(self):
        mgr = TurnManager()
        turn = mgr.create_turn("sess_test", 0)

        assert mgr.transition(turn.turn_id, TurnState.ASR_PROCESSING)
        assert mgr.get_turn(turn.turn_id).state == TurnState.ASR_PROCESSING

        assert mgr.transition(turn.turn_id, TurnState.LLM_THINKING)
        assert mgr.transition(turn.turn_id, TurnState.TTS_SYNTHESIZING)
        assert mgr.transition(turn.turn_id, TurnState.PLAYING)
        assert mgr.transition(turn.turn_id, TurnState.COMPLETED)

        assert not mgr.get_turn(turn.turn_id).is_active

    def test_invalid_transition_rejected(self):
        mgr = TurnManager()
        turn = mgr.create_turn("sess_test", 0)

        # Can't go from STARTED directly to COMPLETED
        assert not mgr.transition(turn.turn_id, TurnState.COMPLETED)

    def test_interruption_flow(self):
        mgr = TurnManager()
        turn1 = mgr.create_turn("sess_test", 0)
        mgr.transition(turn1.turn_id, TurnState.ASR_PROCESSING)
        mgr.transition(turn1.turn_id, TurnState.LLM_THINKING)
        mgr.transition(turn1.turn_id, TurnState.PLAYING)

        # Interrupt with new turn
        turn2 = mgr.create_turn("sess_test", 1)

        # turn1 should be interrupted
        t1 = mgr.get_turn(turn1.turn_id)
        assert t1.state == TurnState.INTERRUPTED
        assert not t1.is_active

        # turn2 should be active
        t2 = mgr.get_turn(turn2.turn_id)
        assert t2.state == TurnState.STARTED

    def test_record_text(self):
        mgr = TurnManager()
        turn = mgr.create_turn("sess_test", 0)
        mgr.record_user_text(turn.turn_id, "你好")
        mgr.record_companion_text(turn.turn_id, "你好呀！")

        t = mgr.get_turn(turn.turn_id)
        assert t.user_text == "你好"
        assert t.companion_text == "你好呀！"

    def test_failed_persistence_can_replace_uncommitted_terminal_claim(self):
        mgr = TurnManager()
        completed = mgr.create_turn("sess_test", 0)
        mgr.transition(completed.turn_id, TurnState.LLM_THINKING)
        mgr.transition(completed.turn_id, TurnState.TTS_SYNTHESIZING)
        assert mgr.transition(completed.turn_id, TurnState.COMPLETED)

        assert mgr.fail_turn(completed.turn_id) is None
        assert mgr.fail_turn(completed.turn_id, allow_completed=True) is completed
        assert completed.state == TurnState.ERROR

        interrupted = mgr.create_turn("sess_test", 1)
        mgr.transition(interrupted.turn_id, TurnState.LLM_THINKING)
        mgr.interrupt_turn(interrupted.turn_id)

        assert mgr.fail_turn(interrupted.turn_id) is None
        assert mgr.fail_turn(interrupted.turn_id, allow_interrupted=True) is interrupted
        assert interrupted.state == TurnState.ERROR


class TestAudioConfirmation:
    """Audio confirmation tracks what user actually heard."""

    def test_full_playback(self):
        from companion.protocols.audio import AudioConfirmationProtocol

        proto = AudioConfirmationProtocol()
        audio = b"mock audio bytes for testing"
        proto.record_synthesis("turn_1", 0, audio, 1000, "Hello world!")
        record = proto.confirm_played("turn_1", 0, 1000)

        assert record is not None
        assert record.was_fully_played
        assert record.effective_text == "Hello world!"

        played = proto.get_played_text("turn_1")
        assert played == "Hello world!"

    def test_interrupted_playback(self):
        from companion.protocols.audio import AudioConfirmationProtocol

        proto = AudioConfirmationProtocol()
        audio = b"mock audio bytes " * 100
        proto.record_synthesis(
            "turn_1", 0, audio, 3000, "This is a longer response that gets interrupted"
        )

        # Interrupted after 1500ms (50%)
        record = proto.confirm_played("turn_1", 0, 1500, was_interrupted=True)

        assert record is not None
        assert record.was_interrupted
        assert not record.was_fully_played
        assert record.played_fraction == 0.5

        # Played text should only include what was actually heard
        played = proto.get_played_text("turn_1")
        assert len(played) < len("This is a longer response that gets interrupted")

    def test_multiple_segments(self):
        from companion.protocols.audio import AudioConfirmationProtocol

        proto = AudioConfirmationProtocol()
        proto.record_synthesis("turn_1", 0, b"audio1", 500, "First part. ")
        proto.record_synthesis("turn_1", 1, b"audio2", 500, "Second part.")

        proto.confirm_played("turn_1", 0, 500)
        proto.confirm_played("turn_1", 1, 200, was_interrupted=True)  # Only 40% played

        played = proto.get_played_text("turn_1")
        assert "First part" in played

    def test_no_audio_played(self):
        from companion.protocols.audio import AudioConfirmationProtocol

        proto = AudioConfirmationProtocol()
        proto.record_synthesis("turn_1", 0, b"audio", 500, "Unheard text")

        assert not proto.was_any_audio_played("turn_1")
        assert proto.get_played_text("turn_1") == ""
        assert proto.get_all_text("turn_1") == "Unheard text"
