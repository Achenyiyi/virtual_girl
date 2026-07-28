"""Affect State — the companion's continuous emotional model.

Uses a low-dimensional continuous state instead of discrete emotion labels.
This avoids the jarring effect of sudden emotion switches and enables
smooth transitions that feel more natural.

Each dimension:
- Has a bounded range
- Receives small deltas from events (capped)
- Decays toward a baseline over time
- Is read by TTS, avatar, and dialogue style renderer at the same time

From the PLAN:
"情绪应是有惯性的状态，而不是每句话贴标签"
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar


@dataclass(frozen=True)
class AffectState:
    """Continuous emotional/arousal state of the companion.

    All values are in [0, 1] or [-1, 1] as specified per field.
    """

    # Core dimensions
    valence: float = 0.0  # [-1, 1] Pleasantness: -1=unpleasant, +1=pleasant
    arousal: float = 0.5  # [0, 1] Activation: 0=calm, 1=excited
    trust: float = 0.5  # [0, 1] Trust in the current situation
    closeness: float = 0.3  # [0, 1] Perceived closeness
    energy: float = 0.7  # [0, 1] Current energy/alertness
    uncertainty: float = 0.3  # [0, 1] Uncertainty about the situation

    # Baseline values (the state drifts toward these)
    baseline_valence: float = 0.0
    baseline_arousal: float = 0.5
    baseline_trust: float = 0.5
    baseline_closeness: float = 0.3
    baseline_energy: float = 0.7
    baseline_uncertainty: float = 0.3

    # Decay rate per second toward baseline
    DECAY_RATE: ClassVar[float] = 0.001  # ~1% of distance per second

    # Metadata
    version: int = 0
    last_updated_at: datetime | None = None
    last_trigger_event_id: str = ""

    # ── Delta bounds (max change per event) ──────────────────────────

    MAX_DELTA_VALENCE: ClassVar[float] = 0.3
    MAX_DELTA_AROUSAL: ClassVar[float] = 0.2
    MAX_DELTA_TRUST: ClassVar[float] = 0.1
    MAX_DELTA_CLOSENESS: ClassVar[float] = 0.05
    MAX_DELTA_ENERGY: ClassVar[float] = 0.3
    MAX_DELTA_UNCERTAINTY: ClassVar[float] = 0.3

    def apply_event(
        self,
        delta_valence: float = 0.0,
        delta_arousal: float = 0.0,
        delta_trust: float = 0.0,
        delta_closeness: float = 0.0,
        delta_energy: float = 0.0,
        delta_uncertainty: float = 0.0,
        event_id: str = "",
    ) -> AffectState:
        """Create a new state with bounded deltas applied."""

        def clamp_and_apply(
            current: float, delta: float, max_delta: float, lo: float, hi: float
        ) -> float:
            bounded = max(-max_delta, min(max_delta, delta))
            return max(lo, min(hi, current + bounded))

        return AffectState(
            valence=clamp_and_apply(self.valence, delta_valence, self.MAX_DELTA_VALENCE, -1.0, 1.0),
            arousal=clamp_and_apply(self.arousal, delta_arousal, self.MAX_DELTA_AROUSAL, 0.0, 1.0),
            trust=clamp_and_apply(self.trust, delta_trust, self.MAX_DELTA_TRUST, 0.0, 1.0),
            closeness=clamp_and_apply(
                self.closeness, delta_closeness, self.MAX_DELTA_CLOSENESS, 0.0, 1.0
            ),
            energy=clamp_and_apply(self.energy, delta_energy, self.MAX_DELTA_ENERGY, 0.0, 1.0),
            uncertainty=clamp_and_apply(
                self.uncertainty, delta_uncertainty, self.MAX_DELTA_UNCERTAINTY, 0.0, 1.0
            ),
            baseline_valence=self.baseline_valence,
            baseline_arousal=self.baseline_arousal,
            baseline_trust=self.baseline_trust,
            baseline_closeness=self.baseline_closeness,
            baseline_energy=self.baseline_energy,
            baseline_uncertainty=self.baseline_uncertainty,
            version=self.version + 1,
            last_updated_at=datetime.now(),
            last_trigger_event_id=event_id,
        )

    def apply_time_decay(self, seconds: float) -> AffectState:
        """Decay each dimension toward its baseline.

        decay(t) = current + (baseline - current) * (1 - e^(-rate * t))
        """
        if seconds <= 0:
            return self

        factor = 1 - math.exp(-self.DECAY_RATE * seconds)

        def decay(current: float, baseline: float, lo: float, hi: float) -> float:
            return max(lo, min(hi, current + (baseline - current) * factor))

        return AffectState(
            valence=decay(self.valence, self.baseline_valence, -1.0, 1.0),
            arousal=decay(self.arousal, self.baseline_arousal, 0.0, 1.0),
            trust=decay(self.trust, self.baseline_trust, 0.0, 1.0),
            closeness=decay(self.closeness, self.baseline_closeness, 0.0, 1.0),
            energy=decay(self.energy, self.baseline_energy, 0.0, 1.0),
            uncertainty=decay(self.uncertainty, self.baseline_uncertainty, 0.0, 1.0),
            baseline_valence=self.baseline_valence,
            baseline_arousal=self.baseline_arousal,
            baseline_trust=self.baseline_trust,
            baseline_closeness=self.baseline_closeness,
            baseline_energy=self.baseline_energy,
            baseline_uncertainty=self.baseline_uncertainty,
            version=self.version,
            last_updated_at=datetime.now(),
            last_trigger_event_id="time_decay",
        )

    def dominant_emotion(self) -> str:
        """Map continuous state to the closest named emotion.

        Uses the circumplex model of affect:
        - High valence + high arousal = happy/excited
        - High valence + low arousal = content/calm
        - Low valence + high arousal = upset/angry
        - Low valence + low arousal = sad/tired
        """
        if self.valence > 0.2:
            return "excited" if self.arousal > 0.6 else "happy" if self.arousal > 0.4 else "content"
        elif self.valence < -0.2:
            return "upset" if self.arousal > 0.6 else "sad" if self.arousal > 0.3 else "tired"
        else:
            return (
                "attentive" if self.arousal > 0.6 else "neutral" if self.arousal > 0.3 else "sleepy"
            )
