"""Avatar provider interface — Live2D/VRM character rendering.

The avatar is the "body" of the companion. It:
- Renders the character on screen (Live2D 3.x / VRM)
- Maps internal state to facial expressions, gestures, posture
- Handles idle animations (breathing, blinking, gaze)
- Responds to proactive behavior levels (Level 0-4)

Per the PLAN, the avatar is a presentation layer only —
it does not hold personality or memory state.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field

from companion.providers.base import Provider, ProviderHealth, ProviderInfo


@dataclass
class FacialExpression:
    """Facial expression parameters for Live2D/VRM."""

    expression_id: str  # e.g., 'happy', 'sad', 'surprised', 'neutral'
    intensity: float = 0.5  # 0 to 1
    # Emotion-mapped parameters
    mouth_open: float = 0.0  # For lip-sync
    eye_open: float = 1.0
    brow_raise: float = 0.0
    cheek_raise: float = 0.0


@dataclass
class BodyPose:
    """Body posture and gesture parameters."""

    pose_id: str = "idle_standing"  # 'idle_standing', 'idle_sitting', 'leaning_forward'
    gesture_id: str | None = None  # 'wave', 'nod', 'head_tilt', 'shrug'
    gesture_intensity: float = 0.5
    breathing_amplitude: float = 0.3  # For idle breathing animation


@dataclass
class EyeBehavior:
    """Eye/gaze behavior parameters."""

    gaze_target: str = "user"  # 'user', 'camera', 'screen', 'away', 'random'
    blink_rate: float = 1.0  # Blinks per ~4 seconds
    eye_contact_duration: float = 3.0  # Seconds before looking away
    pupil_dilation: float = 0.5  # 0 (constricted) to 1 (dilated)


@dataclass
class AvatarState:
    """The complete visual state of the avatar at a point in time."""

    expression: FacialExpression = field(
        default_factory=lambda: FacialExpression(expression_id="neutral")
    )
    pose: BodyPose = field(default_factory=BodyPose)
    eyes: EyeBehavior = field(default_factory=EyeBehavior)

    # Inferred from affect state
    valence: float = 0.0
    arousal: float = 0.5
    energy: float = 0.5

    # Lip-sync
    is_speaking: bool = False
    audio_level: float = 0.0  # Current audio amplitude for lip-sync


@dataclass
class AvatarModel:
    """An avatar model available for loading."""

    model_id: str
    name: str
    type: str  # 'live2d', 'vrm'
    path: str
    thumbnail_path: str | None = None
    expressions: list[str] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)


class AvatarProvider(Provider):
    """Abstract interface for character rendering providers.

    Implementations:
    - AIRIAvatarProvider: integrates with AIRI's Live2D/VRM stage
    - ThreeVRMAvatarProvider: direct three-vrm rendering
    - NullAvatarProvider: no visual (for headless/test mode)
    """

    @abstractmethod
    async def load_model(self, model_id: str) -> bool:
        """Load and display a character model. Returns True on success."""
        ...

    @abstractmethod
    async def update_state(self, state: AvatarState) -> None:
        """Update the avatar's visual state (expression, pose, gaze)."""
        ...

    @abstractmethod
    async def trigger_expression(
        self, expression_id: str, intensity: float = 0.5, duration_ms: int = 2000
    ) -> None:
        """Trigger a facial expression with decay."""
        ...

    @abstractmethod
    async def trigger_gesture(self, gesture_id: str, intensity: float = 0.5) -> None:
        """Trigger a one-shot gesture (wave, nod, etc.)."""
        ...

    @abstractmethod
    async def set_proactive_level(self, level: int) -> None:
        """Set the avatar's proactive behavior level (0-4).

        Level 0: breathing, blinking, idle posture (always active)
        Level 1: facial expressions, silent bubbles
        Level 2: short text hint, glance at screen
        Level 3: active speech, full attention
        Level 4: action proposal posture
        """
        ...

    @abstractmethod
    async def list_available_models(self) -> list[AvatarModel]:
        """List and validate all available avatar models."""
        ...

    @abstractmethod
    async def validate_model(self, model_id: str) -> list[str]:
        """Validate a model and return any errors found."""
        ...

    @abstractmethod
    def provider_info(self) -> ProviderInfo: ...

    @abstractmethod
    async def health_check(self) -> ProviderHealth: ...

    @abstractmethod
    async def shutdown(self) -> None: ...
