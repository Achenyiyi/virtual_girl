"""Expression Mapper — maps internal affect state to external expressions.

Phase 3 core component. Translates continuous emotional state (valence,
arousal, trust, etc.) into:
- Live2D/VRM facial expression parameters
- TTS emotion parameters (style, rate, pitch)
- Avatar gesture and posture suggestions
- Proactive behavior level recommendations

Key design from the PLAN:
"TTS情绪、Live2D表情、动作幅度和措辞都读取同一状态快照"
"""

from __future__ import annotations

from dataclasses import dataclass, field

from companion.schemas.affect import AffectState


@dataclass
class FacialParams:
    """Live2D/VRM facial expression parameters."""

    expression_id: str = "neutral"
    expression_intensity: float = 0.5
    mouth_open: float = 0.0
    eye_open: float = 1.0
    brow_raise: float = 0.0
    cheek_raise: float = 0.0
    eye_squint: float = 0.0
    mouth_smile: float = 0.0
    mouth_frown: float = 0.0


@dataclass
class VoiceParams:
    """TTS voice emotion parameters."""

    style: str = "general"
    rate: float = 1.0
    pitch: float = 1.0
    volume: float = 1.0
    emphasis: float = 0.0  # How much emotional inflection


@dataclass
class GestureSuggestion:
    """Suggested gesture for avatar."""

    gesture_id: str = ""
    intensity: float = 0.5
    priority: int = 0  # Higher = more likely to execute
    duration_ms: int = 2000


@dataclass
class ExpressionSnapshot:
    """Complete expression state at a point in time, derived from affect."""

    facial: FacialParams = field(default_factory=FacialParams)
    voice: VoiceParams = field(default_factory=VoiceParams)
    gestures: list[GestureSuggestion] = field(default_factory=list)
    proactive_level_hint: int = 0

    # Source affect state for debugging
    source_valence: float = 0.0
    source_arousal: float = 0.5
    source_energy: float = 0.7
    source_trust: float = 0.5


class ExpressionMapper:
    """Maps continuous affect state to multimodal expression parameters.

    The mapper reads a single AffectState snapshot and produces consistent
    parameters for all output channels, ensuring the companion's voice,
    face, and gestures tell the same emotional story.
    """

    # Expression mapping table: (valence_min, valence_max, arousal_min, arousal_max) → expression_id
    EXPRESSION_MAP: list[tuple[float, float, float, float, str]] = [
        # (v_min, v_max, a_min, a_max, expression)
        (0.3, 1.0, 0.6, 1.0, "happy"),  # High valence + high arousal = happy
        (0.3, 1.0, 0.3, 0.6, "gentle_smile"),  # High valence + mid arousal = gentle
        (0.3, 1.0, 0.0, 0.3, "content"),  # High valence + low arousal = content
        (-0.2, 0.3, 0.5, 1.0, "attentive"),  # Neutral valence + high arousal = attentive
        (-0.2, 0.3, 0.0, 0.5, "neutral"),  # Neutral valence + low arousal = neutral
        (-0.6, -0.2, 0.5, 1.0, "worried"),  # Low valence + high arousal = worried
        (-1.0, -0.2, 0.0, 0.5, "sad"),  # Low valence + low arousal = sad
        (-1.0, -0.6, 0.5, 1.0, "upset"),  # Very low valence + high arousal = upset
    ]

    def map(self, affect: AffectState) -> ExpressionSnapshot:
        """Map affect state to a complete expression snapshot."""
        facial = self._map_facial(affect)
        voice = self._map_voice(affect)
        gestures = self._map_gestures(affect)
        proactive_hint = self._map_proactive_level(affect)

        return ExpressionSnapshot(
            facial=facial,
            voice=voice,
            gestures=gestures,
            proactive_level_hint=proactive_hint,
            source_valence=affect.valence,
            source_arousal=affect.arousal,
            source_energy=affect.energy,
            source_trust=affect.trust,
        )

    def map_for_tts(self, affect: AffectState) -> VoiceParams:
        """Map affect to TTS voice parameters only."""
        return self._map_voice(affect)

    def map_for_avatar(self, affect: AffectState) -> FacialParams:
        """Map affect to facial expression parameters only."""
        return self._map_facial(affect)

    # ── Internal mappers ──────────────────────────────────────────────

    def _map_facial(self, a: AffectState) -> FacialParams:
        """Map affect to Live2D/VRM facial parameters."""
        expression_id = self._find_expression(a.valence, a.arousal)
        intensity = min(1.0, abs(a.valence) * 0.5 + a.arousal * 0.5)

        params = FacialParams(
            expression_id=expression_id,
            expression_intensity=intensity,
            eye_open=0.5 + a.arousal * 0.5,
            brow_raise=abs(a.valence) * 0.3 if a.valence > 0 else 0.1,
            cheek_raise=max(0, a.valence) * 0.8,
            mouth_smile=max(0, a.valence) * 0.9,
            mouth_frown=max(0, -a.valence) * 0.6,
            mouth_open=0.0,  # Set by lip-sync, not emotion
        )

        # Energy affects eye openness
        params.eye_open = 0.3 + a.energy * 0.7

        return params

    def _map_voice(self, a: AffectState) -> VoiceParams:
        """Map affect to voice synthesis parameters."""
        style = "general"

        if a.valence > 0.3:
            style = "cheerful" if a.arousal > 0.5 else "gentle"
        elif a.valence < -0.3:
            style = "sad" if a.arousal < 0.5 else "empathetic"
        elif a.arousal > 0.6:
            style = "excited"

        # Rate: faster when aroused/energetic
        rate = 0.85 + a.arousal * 0.3 + a.energy * 0.15
        rate = max(0.7, min(1.4, rate))

        # Pitch: higher when happy/excited
        pitch = 0.9 + a.valence * 0.1 + a.arousal * 0.1
        pitch = max(0.8, min(1.2, pitch))

        # Volume: proportional to energy
        volume = 0.6 + a.energy * 0.4

        # Emphasis: stronger when emotional
        emphasis = abs(a.valence) * 0.4 + abs(a.arousal - 0.5) * 0.6

        return VoiceParams(
            style=style,
            rate=round(rate, 2),
            pitch=round(pitch, 2),
            volume=round(volume, 2),
            emphasis=round(emphasis, 2),
        )

    def _map_gestures(self, a: AffectState) -> list[GestureSuggestion]:
        """Suggest gestures based on emotional state."""
        gestures: list[GestureSuggestion] = []

        # Happy/excited → wave, bounce
        if a.valence > 0.3 and a.arousal > 0.5:
            gestures.append(GestureSuggestion("wave", 0.6, priority=1))
            gestures.append(GestureSuggestion("bounce", 0.4, priority=0))

        # Sad/tired → head tilt, slow blink
        if a.valence < -0.2:
            gestures.append(GestureSuggestion("head_tilt", 0.4, priority=1))
            if a.energy < 0.3:
                gestures.append(GestureSuggestion("slow_blink", 0.5, priority=2))

        # Attentive/interested → lean forward, nod
        if a.arousal > 0.6 and a.uncertainty < 0.3:
            gestures.append(GestureSuggestion("lean_forward", 0.5, priority=1))
            gestures.append(GestureSuggestion("nod", 0.3, priority=1))

        # Uncertain → look away briefly
        if a.uncertainty > 0.6:
            gestures.append(GestureSuggestion("look_away", 0.4, priority=2))

        # Trusting → relaxed posture
        if a.trust > 0.7:
            gestures.append(GestureSuggestion("relaxed_pose", 0.5, priority=0))

        return gestures

    def _map_proactive_level(self, a: AffectState) -> int:
        """Recommend a proactive behavior level based on emotional state.

        Level 0: idle/baseline
        Level 1: subtle (expression changes)
        Level 2: hint (text bubble, glance)
        Level 3: conversation (initiate speech)
        Level 4: action proposal
        """
        # High energy + high trust → more proactive
        score = a.energy * 0.3 + a.trust * 0.3 + (1 - a.uncertainty) * 0.2 + a.arousal * 0.2

        # Level 3 and 4 are only triggered by external events, not emotion alone
        # (per the PLAN: "LLM 只负责提出候选内容，不得自行决定")
        # This mapper only recommends up to Level 2 based on emotional state.
        if score > 0.8:
            return 2  # Hint level
        elif score > 0.5:
            return 1  # Subtle
        else:
            return 0  # Idle

    @staticmethod
    def _find_expression(valence: float, arousal: float) -> str:
        """Find the closest matching expression for given affect values."""
        for v_min, v_max, a_min, a_max, expr_id in ExpressionMapper.EXPRESSION_MAP:
            if v_min <= valence <= v_max and a_min <= arousal <= a_max:
                return expr_id
        return "neutral"
